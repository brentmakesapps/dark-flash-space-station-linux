"""GTK 4 / Libadwaita desktop controller for the Space Station."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

from .controller import SpaceStation
from .media import format_temperature
from .openrgb import argb_v2_3_color
from .layout import (
    DISPLAY_SIZE,
    GRAPH_METRICS,
    GRAPH_STYLES,
    Layout,
    LayoutDraft,
    LayoutStore,
    WIDGETS,
    resize_values,
    snap_to_grid,
    widget_selector_state,
)
from .service import service_active, start_service, stop_service
from .settings import AppSettings, SettingsStore
from .telemetry import detected_gpus

PREVIEW_GRID_SPACING = 40
FALLBACK_FONTS = ("Liberation Sans", "DejaVu Sans", "Monospace", "Serif")


def installed_font_families() -> list[str]:
    """Return Fontconfig families for selectable text widgets."""
    try:
        result = subprocess.run(
            ["fc-list", ":", "family"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return list(FALLBACK_FONTS)
    families = set(FALLBACK_FONTS)
    for line in result.stdout.splitlines():
        families.update(
            name.strip().replace(r"\-", "-") for name in line.split(",") if name.strip()
        )
    return sorted(families, key=str.casefold)


def main() -> None:
    """Launch the optional desktop application."""
    try:
        import gi

        gi.require_version("Adw", "1")
        gi.require_version("Gtk", "4.0")
        from gi.repository import Adw, Gdk, Gio, GLib, Gtk
    except (ImportError, ValueError) as exc:
        raise SystemExit(
            "GTK 4 and Libadwaita bindings are required; install python-gobject and libadwaita"
        ) from exc

    class SpaceStationApplication(Adw.Application):
        def __init__(self) -> None:
            super().__init__(
                application_id="com.darkflash.SpaceStation",
                flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
            )
            self.controller = SpaceStation()
            self.layout_store = LayoutStore()
            self.layout_draft = LayoutDraft(self.layout_store)
            self.settings_store = SettingsStore()
            self.settings = self.settings_store.load()
            if self.settings.selected_layout in self.layout_draft.layouts:
                self.layout_draft.select(
                    self.settings.selected_layout, mark_dirty=False
                )
            self.layouts = self.layout_draft.layouts
            self.layout = self.layout_draft.active_layout
            self.selected_widget = 0
            self.preview_font: str | None = None
            self.updating_layout_controls = False
            self.resetting_layout = False
            self.telemetry_stop = threading.Event()
            self.gif_sync_stop = threading.Event()
            self.gif_sync_source: Path | None = None
            self.gif_sync_color: str | None = None
            self.window: Adw.ApplicationWindow | None = None
            self.toast_overlay: Adw.ToastOverlay | None = None
            self.status_label: Gtk.Label | None = None
            self.telemetry_button: Gtk.Button | None = None
            self.service_status_label: Gtk.Label | None = None
            self.service_button: Gtk.Button | None = None
            self.save_layout_button: Gtk.Button | None = None
            self.reset_layout_button: Gtk.Button | None = None
            self.gpus = detected_gpus()
            self.gpu_bdfs = [gpu.bdf for gpu in self.gpus]
            gpu_labels = [
                f"{gpu.label} · {gpu.bdf} ({gpu.driver})" for gpu in self.gpus
            ] or ["No display GPUs detected"]
            self.gpu_selector = Gtk.DropDown.new_from_strings(gpu_labels)
            if self.settings.gpu_bdf in self.gpu_bdfs:
                self.gpu_selector.set_selected(
                    self.gpu_bdfs.index(self.settings.gpu_bdf)
                )
            self.gpu_selector.set_sensitive(bool(self.gpu_bdfs))
            self.temperature_unit = Gtk.DropDown.new_from_strings(
                ["Celsius (°C)", "Fahrenheit (°F)"]
            )
            self.temperature_unit.set_selected(
                0 if self.settings.temperature_unit == "C" else 1
            )
            self.interval = Gtk.SpinButton.new_with_range(0.2, 60, 0.1)
            self.interval.set_value(self.settings.refresh_interval)
            self.timeout = Gtk.SpinButton.new_with_range(0, 3600, 1)
            self.timeout.set_value(self.settings.sleep_timeout)
            self.layout_selector = Gtk.DropDown.new_from_strings(list(self.layouts))
            self.layout_selector.set_selected(list(self.layouts).index(self.layout.name))
            self.widget_selector = Gtk.DropDown.new_from_strings([])
            self.fonts = installed_font_families()
            self.font_selector = Gtk.DropDown.new_from_strings(self.fonts)
            font_factory = Gtk.SignalListItemFactory()
            font_factory.connect("setup", self._setup_font_item)
            font_factory.connect("bind", self._bind_font_item)
            self.font_selector.set_factory(font_factory)
            self.font_size = Gtk.SpinButton.new_with_range(8, 64, 1)
            self.graph_width = Gtk.SpinButton.new_with_range(20, 320, 1)
            self.graph_height = Gtk.SpinButton.new_with_range(20, 320, 1)
            self.graph_history_seconds = Gtk.SpinButton.new_with_range(1, 3600, 1)
            color_dialog = Gtk.ColorDialog.new()
            color_dialog.set_title("Widget color")
            self.color = Gtk.ColorDialogButton.new(color_dialog)
            self.custom_text = Gtk.Entry(placeholder_text="Custom text")
            self.gif_mask_mode = Gtk.DropDown.new_from_strings(
                ["No mask", "Manual color", "OpenRGB ARGB_V2_3"]
            )
            self.gif_mask_mode.set_selected(
                {"none": 0, "manual": 1, "openrgb": 2}[self.settings.gif_mask_mode]
            )
            gif_mask_dialog = Gtk.ColorDialog.new()
            gif_mask_dialog.set_title("GIF mask color")
            self.gif_mask_color = Gtk.ColorDialogButton.new(gif_mask_dialog)
            mask_rgba = Gdk.RGBA()
            mask_rgba.parse(self.settings.gif_mask_color)
            self.gif_mask_color.set_rgba(mask_rgba)
            self.gif_sync = Gtk.Switch(valign=Gtk.Align.CENTER)
            self.grid_snap = Gtk.Switch(
                active=self.settings.snap_to_grid, valign=Gtk.Align.CENTER
            )
            self.preview = Gtk.Fixed()
            self.preview.set_size_request(DISPLAY_SIZE, DISPLAY_SIZE)
            self.preview.set_hexpand(False)
            self.preview.set_halign(Gtk.Align.START)
            self.preview.set_overflow(Gtk.Overflow.HIDDEN)
            self.preview.set_focusable(True)
            preview_keys = Gtk.EventControllerKey()
            preview_keys.connect("key-pressed", self._delete_key_pressed)
            self.preview.add_controller(preview_keys)
            self.preview.add_css_class("preview-canvas")
            self.preview_grid = Gtk.DrawingArea()
            self.preview_grid.set_content_width(DISPLAY_SIZE)
            self.preview_grid.set_content_height(DISPLAY_SIZE)
            self.preview_grid.set_can_target(False)
            self.preview_grid.set_draw_func(self._draw_preview_grid)
            self.preview.put(self.preview_grid, 0, 0)
            self.preview_labels: list[Gtk.Widget] = []
            self.preview_values: list[Gtk.Widget] = []
            self._resize_start: tuple[int, int, int, int] | None = None
            self._resize_values: tuple[int, int, int] | None = None
            self._drag_start: tuple[int, int, int] | None = None
            self._drag_delta: tuple[float, float] = (0, 0)
            self._interaction_ghost: Gtk.Widget | None = None

        def do_activate(self) -> None:
            if self.window is not None:
                self.window.present()
                return
            self.window = Adw.ApplicationWindow(application=self, title="Space Station")
            self.window.set_default_size(620, 620)
            font_keys = Gtk.EventControllerKey()
            font_keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            font_keys.connect("key-pressed", self._preview_font_key)
            self.window.add_controller(font_keys)
            self.toast_overlay = Adw.ToastOverlay()
            toolbar = Adw.ToolbarView()
            header = Adw.HeaderBar()
            toolbar.add_top_bar(header)
            toolbar.set_content(self._preferences_page())
            self.toast_overlay.set_child(toolbar)
            self.window.set_content(self.toast_overlay)
            self._install_preview_styles()
            self.window.present()
            self._refresh_status()
            self._refresh_service_status()

        def do_shutdown(self) -> None:
            self.telemetry_stop.set()
            self.gif_sync_stop.set()
            super().do_shutdown()

        def _preferences_page(self) -> Adw.PreferencesPage:
            page = Adw.PreferencesPage()
            page.add(self._status_group())
            page.add(self._media_group())
            page.add(self._display_group())
            page.add(self._telemetry_group())
            page.add(self._layout_group())
            return page

        def _status_group(self) -> Adw.PreferencesGroup:
            group = Adw.PreferencesGroup(title="Device")
            row = Adw.ActionRow(title="Connection status")
            self.status_label = Gtk.Label(label="Checking device…")
            self.status_label.add_css_class("dim-label")
            row.add_suffix(self.status_label)
            group.add(row)
            refresh = Adw.ActionRow(title="Refresh status")
            button = Gtk.Button(label="Refresh", valign=Gtk.Align.CENTER)
            button.connect("clicked", lambda *_: self._refresh_status())
            refresh.add_suffix(button)
            group.add(refresh)
            wake = Adw.ActionRow(title="Wake display", subtitle="Resume the display and apply its timeout")
            button = Gtk.Button(label="Wake", valign=Gtk.Align.CENTER)
            button.connect("clicked", lambda *_: self._run(self._wake))
            wake.add_suffix(button)
            group.add(wake)
            return group

        def _media_group(self) -> Adw.PreferencesGroup:
            group = Adw.PreferencesGroup(
                title="Media",
                description="GIFs are converted to looping H.264 video. Still images occupy the OSD layer.",
            )
            image = Adw.ActionRow(title="Still image", subtitle="Center-crop and upload a PNG OSD")
            button = Gtk.Button(label="Choose image", valign=Gtk.Align.CENTER)
            button.connect("clicked", lambda *_: self._choose_file("image"))
            image.add_suffix(button)
            group.add(image)
            animation = Adw.ActionRow(title="Animated GIF", subtitle="Upload as a looping MP4 background")
            button = Gtk.Button(label="Choose GIF", valign=Gtk.Align.CENTER)
            button.connect("clicked", lambda *_: self._choose_file("gif"))
            animation.add_suffix(button)
            group.add(animation)
            mask = Adw.ActionRow(
                title="GIF color mask",
                subtitle="Tint grayscale GIFs with a manual color or OpenRGB ARGB_V2_3.",
            )
            mask.add_suffix(self.gif_mask_mode)
            mask.add_suffix(self.gif_mask_color)
            group.add(mask)
            self.gif_mask_mode.connect(
                "notify::selected", lambda *_: self._save_gif_mask_settings()
            )
            self.gif_mask_color.connect(
                "notify::rgba", lambda *_: self._save_gif_mask_settings()
            )
            sync = Adw.ActionRow(
                title="Sync GIF with OpenRGB",
                subtitle="Re-uploads the last GIF after a stable ARGB_V2_3 color change.",
            )
            self.gif_sync.connect("notify::active", lambda *_: self._toggle_gif_sync())
            sync.add_suffix(self.gif_sync)
            group.add(sync)
            clear = Adw.ActionRow(title="Clear foreground overlay", subtitle="Reveal the current video background")
            button = Gtk.Button(label="Clear", valign=Gtk.Align.CENTER)
            button.connect("clicked", lambda *_: self._run(self.controller.clear_overlay))
            clear.add_suffix(button)
            group.add(clear)
            return group

        def _display_group(self) -> Adw.PreferencesGroup:
            group = Adw.PreferencesGroup(
                title="Display",
                description="Brightness and rotation are reported by the device but are read-only until their firmware commands are captured.",
            )
            row = Adw.ActionRow(title="Sleep timeout", subtitle="Seconds before the display sleeps; 0 disables sleep")
            row.add_suffix(self.timeout)
            button = Gtk.Button(label="Apply", valign=Gtk.Align.CENTER)
            button.connect("clicked", lambda *_: self._run(self._set_timeout))
            row.add_suffix(button)
            group.add(row)
            return group

        def _telemetry_group(self) -> Adw.PreferencesGroup:
            group = Adw.PreferencesGroup(
                title="Native telemetry",
                description="Renders CPU/GPU readings as a foreground OSD above the current background.",
            )
            bdf = Adw.ActionRow(
                title="GPU",
                subtitle="Detected PCI display controller used for GPU telemetry",
            )
            bdf.add_suffix(self.gpu_selector)
            group.add(bdf)
            temperature_unit = Adw.ActionRow(
                title="Temperature unit",
                subtitle="Used by telemetry overlays and the live preview",
            )
            temperature_unit.add_suffix(self.temperature_unit)
            group.add(temperature_unit)
            self.gpu_selector.connect(
                "notify::selected", lambda *_: self._save_telemetry_settings()
            )
            self.temperature_unit.connect(
                "notify::selected", lambda *_: self._save_telemetry_settings()
            )
            interval = Adw.ActionRow(title="Refresh interval")
            interval.add_suffix(self.interval)
            interval.add_suffix(Gtk.Label(label="seconds"))
            group.add(interval)
            self.interval.connect(
                "value-changed", lambda *_: self._save_telemetry_settings()
            )
            self.timeout.connect(
                "value-changed", lambda *_: self._save_telemetry_settings()
            )
            row = Adw.ActionRow(title="Telemetry stream")
            once = Gtk.Button(label="Send once", valign=Gtk.Align.CENTER)
            once.connect("clicked", lambda *_: self._run(self._telemetry_once))
            row.add_suffix(once)
            self.telemetry_button = Gtk.Button(label="Start", valign=Gtk.Align.CENTER)
            self.telemetry_button.connect("clicked", lambda *_: self._toggle_telemetry())
            row.add_suffix(self.telemetry_button)
            group.add(row)
            service = Adw.ActionRow(
                title="Background overlay service",
                subtitle="Runs the active saved layout continuously and keeps the display awake.",
            )
            self.service_status_label = Gtk.Label(label="Checking…")
            self.service_status_label.add_css_class("dim-label")
            service.add_suffix(self.service_status_label)
            self.service_button = Gtk.Button(label="Start", valign=Gtk.Align.CENTER)
            self.service_button.connect(
                "clicked", lambda *_: self._toggle_background_service()
            )
            service.add_suffix(self.service_button)
            group.add(service)
            return group

        def _layout_group(self) -> Adw.PreferencesGroup:
            group = Adw.PreferencesGroup(title="Telemetry layout", description="Drag widgets to position them; drag a selected widget's lower-right handle to resize it. Changes remain in memory until saved.")
            layout_row = Adw.ActionRow(title="Named layout")
            self.layout_selector.connect("notify::selected", lambda *_: self._select_layout())
            layout_row.add_suffix(self.layout_selector)
            new = Gtk.Button(label="New")
            new.connect("clicked", lambda *_: self._new_layout())
            layout_row.add_suffix(new)
            add = Gtk.MenuButton(label="Add")
            add.set_popover(self._add_widget_popover())
            layout_row.add_suffix(add)
            self.save_layout_button = Gtk.Button(label="Save layout")
            self.save_layout_button.set_sensitive(False)
            self.save_layout_button.connect("clicked", lambda *_: self._save_layouts())
            layout_row.add_suffix(self.save_layout_button)
            self.reset_layout_button = Gtk.Button(label="Reset layout")
            self.reset_layout_button.set_sensitive(False)
            self.reset_layout_button.connect("clicked", lambda *_: self._reset_layouts())
            layout_row.add_suffix(self.reset_layout_button)
            group.add(layout_row)
            preview_row = Adw.ActionRow(title="Live preview")
            preview_row.add_suffix(self.preview)
            group.add(preview_row)
            grid_snap = Adw.ActionRow(
                title="Snap to grid",
                subtitle=f"Snap placement and graph resizing to {PREVIEW_GRID_SPACING}-pixel grid lines.",
            )
            self.grid_snap.connect("notify::active", lambda *_: self._save_grid_snap())
            grid_snap.add_suffix(self.grid_snap)
            group.add(grid_snap)
            controls = Adw.ActionRow(title="Selected widget")
            self._rebuild_widget_selector(self.selected_widget)
            self.widget_selector.connect("notify::selected", lambda *_: self._select_widget())
            controls.add_suffix(self.widget_selector)
            group.add(controls)
            font = Adw.ActionRow(title="Font")
            font.add_suffix(self.font_selector)
            group.add(font)
            self.font_row = font
            size = Adw.ActionRow(title="Font size")
            size.add_suffix(self.font_size)
            group.add(size)
            self.font_size_row = size
            width = Adw.ActionRow(title="Graph width")
            width.add_suffix(self.graph_width)
            group.add(width)
            self.graph_width_row = width
            height = Adw.ActionRow(title="Graph height")
            height.add_suffix(self.graph_height)
            group.add(height)
            self.graph_height_row = height
            history = Adw.ActionRow(
                title="Graph history",
                subtitle="Rolling window in seconds",
            )
            history.add_suffix(self.graph_history_seconds)
            group.add(history)
            self.graph_history_row = history
            color = Adw.ActionRow(title="Color")
            color.add_suffix(self.color)
            group.add(color)
            self.color_row = color
            text = Adw.ActionRow(
                title="Custom text",
                subtitle="Used only by Custom text widgets",
            )
            text.add_suffix(self.custom_text)
            group.add(text)
            self.custom_text_row = text
            self.font_selector.connect("notify::selected", lambda *_: self._style_widget())
            self.font_size.connect("value-changed", lambda *_: self._style_widget())
            self.graph_width.connect(
                "value-changed", lambda *_: self._style_widget("width")
            )
            self.graph_height.connect(
                "value-changed", lambda *_: self._style_widget("height")
            )
            self.graph_history_seconds.connect("value-changed", lambda *_: self._style_widget())
            self.color.connect("notify::rgba", lambda *_: self._style_widget())
            self.custom_text.connect("changed", lambda *_: self._style_widget())
            self._refresh_preview()
            self._select_widget()
            return group

        def _setup_font_item(self, _factory: object, item: object) -> None:
            label = Gtk.Label(xalign=0)
            label.set_margin_start(6)
            label.set_margin_end(6)
            focus = Gtk.EventControllerFocus()
            focus.connect("enter", lambda *_args: self._preview_font_item(item))
            label.add_controller(focus)
            pointer = Gtk.EventControllerMotion()
            pointer.connect("enter", lambda *_args: self._preview_font_item(item))
            label.add_controller(pointer)
            item.set_child(label)

        @staticmethod
        def _bind_font_item(_factory: object, item: object) -> None:
            font = item.get_item().get_string()
            escaped_font = GLib.markup_escape_text(font)
            item.get_child().set_markup(
                f'<span font_desc="{escaped_font}">{escaped_font}</span>'
            )

        def _preview_font_item(self, item: object) -> None:
            font_item = item.get_item()
            if font_item is not None:
                self._preview_font(font_item.get_string())

        def _preview_font_key(
            self, _controller: object, keyval: int, _keycode: int, _state: int
        ) -> bool:
            if not self.font_selector.has_focus():
                return False
            if keyval == Gdk.KEY_Escape:
                self.preview_font = None
                self._refresh_preview()
                return False
            if keyval not in (Gdk.KEY_Up, Gdk.KEY_Down):
                return False
            current = self.preview_font
            if current is None and self.selected_widget < len(self.layout.widgets):
                current = self.layout.widgets[self.selected_widget].font
            try:
                index = self.fonts.index(current) if current is not None else 0
            except ValueError:
                index = 0
            index = max(0, min(len(self.fonts) - 1, index + (1 if keyval == Gdk.KEY_Down else -1)))
            self._preview_font(self.fonts[index])
            return False

        def _preview_font(self, font: str) -> None:
            if self.selected_widget >= len(self.layout.widgets):
                return
            self.preview_font = font
            self._refresh_preview()

        def _select_layout(self) -> None:
            if self.resetting_layout:
                return
            selected = self.layout_selector.get_selected()
            if selected == Gtk.INVALID_LIST_POSITION or selected >= len(self.layouts):
                return
            name = list(self.layouts)[selected]
            if name != self.layout.name and self.layout_draft.dirty:
                self._toast("Unsaved layout changes remain in memory; save to write them to disk.")
            self.layout_draft.select(name, mark_dirty=False)
            self.layout = self.layout_draft.active_layout
            self.settings.selected_layout = name
            self._save_settings()
            if (
                hasattr(self, "gif_sync")
                and self.gif_mask_mode.get_selected() != 2
                and self.gif_sync.get_active()
            ):
                self.gif_sync.set_active(False)

        def _save_grid_snap(self) -> None:
            self.settings.snap_to_grid = self.grid_snap.get_active()
            self._save_settings()
            self.selected_widget = 0
            self._refresh_preview()
            if self._rebuild_widget_selector(self.selected_widget):
                self._select_widget()

        def _new_layout(self) -> None:
            name = f"Layout {len(self.layouts) + 1}"
            self.layouts[name] = Layout(name, [type(widget)(**vars(widget)) for widget in self.layout.widgets])
            self.layout_draft.select(name)
            self.layout = self.layouts[name]
            self.layout_selector.set_model(Gtk.StringList.new(list(self.layouts)))
            self.layout_selector.set_selected(len(self.layouts) - 1)
            self.settings.selected_layout = name
            self._save_settings()
            self._mark_layout_dirty()

        def _add_widget_popover(self) -> Gtk.Popover:
            labels = {
                "cpu-temperature": "CPU temperature",
                "cpu-load": "CPU load",
                "gpu-temperature": "GPU temperature",
                "gpu-load": "GPU load",
                "network-upload": "Network upload",
                "network-download": "Network download",
                "time": "Time",
                "date": "Date",
                "custom-text": "Custom text",
            }
            popover = Gtk.Popover()
            stack = Gtk.Stack()

            def choices_page(items: list[tuple[str, object]]) -> Gtk.ScrolledWindow:
                choices = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                choices.set_size_request(360, -1)
                for label_text, callback in items:
                    button = Gtk.Button(label=label_text, halign=Gtk.Align.FILL)
                    button.set_margin_start(6)
                    button.set_margin_end(6)
                    button.connect("clicked", lambda _button, callback=callback: callback())
                    choices.append(button)
                scroll = Gtk.ScrolledWindow(min_content_width=360, min_content_height=240, max_content_height=420)
                scroll.set_child(choices)
                return scroll

            type_items: list[tuple[str, object]] = [
                ("Text value", lambda: stack.set_visible_child_name("text")),
                *[(f"{style.replace('-', ' ').title()} graph", lambda style=style: stack.set_visible_child_name(style)) for style in GRAPH_STYLES],
                ("Custom text", lambda: (self._add_widget("custom-text"), popover.popdown())),
            ]
            stack.add_named(choices_page(type_items), "types")
            text_metrics = [
                (labels[metric], lambda metric=metric: (self._add_widget(metric), popover.popdown()))
                for metric in WIDGETS if metric != "custom-text"
            ]
            stack.add_named(choices_page([("← Back", lambda: stack.set_visible_child_name("types")), *text_metrics]), "text")
            for style in GRAPH_STYLES:
                metrics = [
                    (labels[metric], lambda metric=metric, style=style: (self._add_widget(metric, graph_style=style), popover.popdown()))
                    for metric in GRAPH_METRICS
                ]
                stack.add_named(choices_page([("← Back", lambda: stack.set_visible_child_name("types")), *metrics]), style)
            popover.set_child(stack)
            return popover

        def _add_widget(self, metric: str, graph_style: str | None = None) -> None:
            from .layout import Widget

            self.layout.widgets.append(
                Widget(
                    metric,
                    16,
                    200,
                    text="Label",
                    graph_style=graph_style,
                    width=120,
                    height=60,
                    history_seconds=60,
                )
            )
            self.selected_widget = len(self.layout.widgets) - 1
            self._mark_layout_dirty()
            if self._rebuild_widget_selector(self.selected_widget):
                self._select_widget()

        def _select_widget(self) -> None:
            if self.updating_layout_controls:
                return
            selected = self.widget_selector.get_selected()
            if selected == Gtk.INVALID_LIST_POSITION or selected >= len(self.layout.widgets):
                return
            self.selected_widget = selected
            widget = self.layout.widgets[self.selected_widget]
            self.preview_font = None
            self.updating_layout_controls = True
            try:
                self.font_selector.set_selected(
                    self.fonts.index(widget.font)
                    if widget.font in self.fonts
                    else Gtk.INVALID_LIST_POSITION
                )
                self.font_size.set_value(widget.size)
                self.graph_width.set_value(widget.width)
                self.graph_height.set_value(widget.height)
                self.graph_history_seconds.set_value(widget.history_seconds)
                rgba = Gdk.RGBA()
                rgba.parse(widget.color)
                self.color.set_rgba(rgba)
                self.custom_text.set_text(widget.text)
            finally:
                self.updating_layout_controls = False
            self._update_editor_visibility(widget)
            self._refresh_preview()

        def _update_editor_visibility(self, widget: Widget) -> None:
            is_graph = widget.is_graph
            is_custom_text = widget.metric == "custom-text"
            self.font_row.set_visible(not is_graph)
            self.font_size_row.set_visible(not is_graph)
            self.graph_width_row.set_visible(is_graph)
            self.graph_height_row.set_visible(is_graph)
            self.graph_history_row.set_visible(is_graph)
            self.color_row.set_visible(True)
            self.custom_text_row.set_visible(is_custom_text)

        def _style_widget(self, changed_dimension: str | None = None) -> None:
            if self.updating_layout_controls or self.selected_widget >= len(self.layout.widgets):
                return
            widget = self.layout.widgets[self.selected_widget]
            selected_font = self.font_selector.get_selected()
            if selected_font == Gtk.INVALID_LIST_POSITION:
                return
            self.preview_font = None
            rgba = self.color.get_rgba()
            color = "#{:02x}{:02x}{:02x}".format(
                round(rgba.red * 255),
                round(rgba.green * 255),
                round(rgba.blue * 255),
            )
            widget.font, widget.size, widget.color, widget.text = self.fonts[selected_font], int(self.font_size.get_value()), color, self.custom_text.get_text()
            width = int(self.graph_width.get_value())
            height = int(self.graph_height.get_value())
            if widget.is_circular_graph:
                side = width if changed_dimension == "width" else height
                widget.width = side
                widget.height = side
                self.updating_layout_controls = True
                try:
                    self.graph_width.set_value(side)
                    self.graph_height.set_value(side)
                finally:
                    self.updating_layout_controls = False
            else:
                widget.width, widget.height = width, height
            widget.history_seconds = int(self.graph_history_seconds.get_value())
            self._mark_layout_dirty()
            self._refresh_preview()

        def _refresh_preview(self) -> None:
            self._clear_interaction_ghost()
            for label in self.preview_labels:
                self.preview.remove(label)
            self.preview_labels = []
            self.preview_values = []
            samples = {
                "cpu-temperature": (
                    "CPU Temperature",
                    format_temperature(42, self._selected_temperature_unit()),
                ),
                "cpu-load": ("CPU Load", "17%"),
                "gpu-temperature": (
                    "GPU Temperature",
                    format_temperature(50, self._selected_temperature_unit()),
                ),
                "gpu-load": ("GPU Load", "8%"),
                "network-upload": ("Network Upload", "UP 2.4 KB/s"),
                "network-download": ("Network Download", "DOWN 15.8 KB/s"),
                "time": ("Time", "18:15"),
                "date": ("Date", "2026-09-26"),
            }
            for index, widget in enumerate(self.layout.widgets):
                name, sample = (
                    ("Custom Text", widget.text)
                    if widget.metric == "custom-text"
                    else samples[widget.metric]
                )
                if widget.is_graph:
                    name = (
                        f"{name} · {widget.graph_style.replace('-', ' ')}"
                        f" · {widget.history_seconds}s"
                    )
                content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                title = Gtk.Label(label=name, halign=Gtk.Align.START)
                title.add_css_class("preview-widget-title")
                title.set_visible(False)
                if widget.is_graph:
                    value = self._preview_graph(widget)
                else:
                    value = Gtk.Label(halign=Gtk.Align.START)
                    font = (
                        self.preview_font
                        if index == self.selected_widget and self.preview_font is not None
                        else widget.font
                    )
                    value.set_markup(
                        f'<span foreground="{widget.color}" font_desc="{font} {widget.size}">{sample}</span>'
                    )
                value.add_css_class("preview-widget")
                if index == self.selected_widget:
                    value.add_css_class("preview-selected")
                content.append(title)
                content.append(value)
                container = Gtk.Overlay()
                container.set_child(content)
                click = Gtk.GestureClick()
                click.connect("released", lambda *_args, index=index: self._select_preview_widget(index))
                container.add_controller(click)
                gesture = Gtk.GestureDrag()
                gesture.connect(
                    "drag-begin",
                    lambda _gesture, _x, _y, index=index: self._begin_drag(index),
                )
                gesture.connect("drag-update", lambda gesture, x, y, index=index: self._drag_widget(index, x, y))
                gesture.connect("drag-end", lambda *_args: self._end_drag())
                container.add_controller(gesture)
                if index == self.selected_widget:
                    handle = Gtk.Box()
                    handle.set_size_request(12, 12)
                    handle.set_halign(Gtk.Align.END)
                    handle.set_valign(Gtk.Align.END)
                    handle.add_css_class("preview-resize-handle")
                    resize = Gtk.GestureDrag()
                    resize.connect(
                        "drag-begin",
                        lambda gesture, _x, _y, index=index: self._begin_resize(
                            gesture, index
                        ),
                    )
                    resize.connect(
                        "drag-update",
                        lambda _gesture, x, y, index=index: self._resize_widget(index, x, y),
                    )
                    resize.connect("drag-end", lambda *_args: self._end_resize())
                    handle.add_controller(resize)
                    container.add_overlay(handle)
                self.preview.put(container, widget.x, widget.y)
                self.preview_labels.append(container)
                self.preview_values.append(value)

        def _rebuild_widget_selector(self, selected: int | None = None) -> bool:
            labels, selected_widget = widget_selector_state(
                self.layout.widgets,
                self.selected_widget if selected is None else selected,
            )
            self.updating_layout_controls = True
            try:
                self.widget_selector.set_model(Gtk.StringList.new(labels))
                if labels:
                    self.selected_widget = selected_widget
                    self.widget_selector.set_selected(self.selected_widget)
                else:
                    self.selected_widget = -1
                    self.widget_selector.set_selected(Gtk.INVALID_LIST_POSITION)
            finally:
                self.updating_layout_controls = False
            return bool(labels)

        def _preview_graph(self, widget: object) -> Gtk.DrawingArea:
            area = Gtk.DrawingArea()
            area.set_content_width(widget.width)
            area.set_content_height(widget.height)

            def draw(_area: Gtk.DrawingArea, context: object, width: int, height: int) -> None:
                sample_values = (0.35, 0.7, 0.45, 0.9, 0.55, 0.75)
                sample_count = max(2, min(24, round(widget.history_seconds / 10)))
                values = tuple(
                    sample_values[index % len(sample_values)]
                    for index in range(sample_count)
                )
                rgba = Gdk.RGBA()
                rgba.parse(widget.color)
                context.set_source_rgba(rgba.red, rgba.green, rgba.blue, 0.22)
                if widget.graph_style == "pie":
                    context.arc(width / 2, height / 2, min(width, height) / 2 - 2, 0, 6.283)
                    context.fill()
                    context.set_source_rgba(rgba.red, rgba.green, rgba.blue, 1)
                    context.move_to(width / 2, height / 2)
                    context.arc(width / 2, height / 2, min(width, height) / 2 - 2, -1.57, 2.2)
                    context.fill()
                    return
                context.set_source_rgba(rgba.red, rgba.green, rgba.blue, 1)
                context.set_line_width(2)
                if widget.graph_style == "bar":
                    for position, value in enumerate(values):
                        bar_width = width / len(values)
                        context.rectangle(position * bar_width, height * (1 - value), bar_width - 1, height * value)
                    context.fill()
                    return
                if widget.graph_style == "circular-line":
                    context.save()
                    context.translate(width / 2, height / 2)
                    context.scale(max(8, width / 2 - 3), max(8, height / 2 - 3))
                    context.arc(0, 0, 1, 0, 6.283)
                    context.restore()
                    context.stroke()
                    return
                if widget.graph_style in {"semicircle-gauge", "ring-gauge"}:
                    ratio = values[-1]
                    context.set_line_width(4)
                    if widget.graph_style == "semicircle-gauge":
                        start, end = 3.1416, 6.283
                    else:
                        start, end = -1.5708, 4.7124
                    if widget.graph_style == "ring-gauge":
                        context.set_source_rgba(
                            rgba.red, rgba.green, rgba.blue, 0.5
                        )
                        context.save()
                        context.translate(width / 2, height / 2)
                        context.scale(
                            max(8, width / 2 - 3), max(8, height / 2 - 3)
                        )
                        context.arc(0, 0, 1, start, end)
                        context.restore()
                        context.stroke()
                        return
                    context.set_source_rgba(1, 1, 1, 0.27)
                    context.save()
                    context.translate(width / 2, height / 2)
                    context.scale(max(8, width / 2 - 3), max(8, height / 2 - 3))
                    context.arc(0, 0, 1, start, end)
                    context.restore()
                    context.stroke()
                    context.set_source_rgba(rgba.red, rgba.green, rgba.blue, 1)
                    context.save()
                    context.translate(width / 2, height / 2)
                    context.scale(max(8, width / 2 - 3), max(8, height / 2 - 3))
                    context.arc(0, 0, 1, start, start + (end - start) * ratio)
                    context.restore()
                    context.stroke()
                    return
                for position, value in enumerate(values):
                    x, y = position * width / (len(values) - 1), height * (1 - value)
                    if position:
                        context.line_to(x, y)
                    else:
                        context.move_to(x, y)
                context.stroke()

            area.set_draw_func(draw)
            return area

        def _select_preview_widget(self, index: int) -> None:
            self.selected_widget = index
            self.preview.grab_focus()
            self.widget_selector.set_selected(index)
            self._select_widget()

        def _delete_key_pressed(
            self, _controller: Gtk.EventControllerKey, keyval: int, _keycode: int, _state: int
        ) -> bool:
            if keyval not in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete):
                return False
            if not self.layout.widgets:
                return True
            del self.layout.widgets[self.selected_widget]
            self.selected_widget = min(self.selected_widget, len(self.layout.widgets) - 1)
            self._mark_layout_dirty()
            if self._rebuild_widget_selector(self.selected_widget):
                self._select_widget()
            else:
                self._refresh_preview()
            return True

        def _install_preview_styles(self) -> None:
            provider = Gtk.CssProvider()
            provider.load_from_string(
                f".preview-canvas {{ min-width: {DISPLAY_SIZE}px; min-height: {DISPLAY_SIZE}px; "
                f"max-width: {DISPLAY_SIZE}px; max-height: {DISPLAY_SIZE}px; "
                "background-color: alpha(#1e1e1e, 0.35); }"
                ".preview-widget { padding: 0; border: 0 solid transparent; }"
                ".preview-widget-title { font-size: 10px; color: #9a9996; }"
                ".preview-selected { background-color: alpha(#3584e4, 0.2); }"
                ".preview-resize-handle { min-width: 12px; min-height: 12px; margin: 1px; "
                "background-color: #3584e4; border: 2px solid #ffffff; border-radius: 6px; }"
                ".preview-interaction-ghost { border: 2px dashed #62a0ea; "
                "background-color: alpha(#62a0ea, 0.18); }"
            )
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(),
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

        def _draw_preview_grid(
            self, _area: Gtk.DrawingArea, context: object, width: int, height: int
        ) -> None:
            context.set_line_width(1)
            context.set_source_rgba(1, 1, 1, 0.14)
            for position in range(0, min(width, height) + 1, PREVIEW_GRID_SPACING):
                coordinate = position + 0.5
                context.move_to(coordinate, 0)
                context.line_to(coordinate, height)
                context.move_to(0, coordinate)
                context.line_to(width, coordinate)
            context.stroke()

        def _drag_widget(self, index: int, x: float, y: float) -> None:
            if (
                self._resize_start is not None
                or self._drag_start is None
                or self._drag_start[0] != index
            ):
                return
            self._drag_delta = (x, y)
            _, start_x, start_y = self._drag_start
            preview_widget = self.preview_labels[index]
            width = max(20, preview_widget.get_width())
            height = max(20, preview_widget.get_height())
            ghost_x, ghost_y = self._bounded_preview_position(
                start_x + x, start_y + y, width, height
            )
            self._show_interaction_ghost(ghost_x, ghost_y, width, height)

        def _begin_drag(self, index: int) -> None:
            widget = self.layout.widgets[index]
            self._drag_start = (index, widget.x, widget.y)
            self._drag_delta = (0, 0)
            preview_widget = self.preview_labels[index]
            self._show_interaction_ghost(
                widget.x,
                widget.y,
                max(20, preview_widget.get_width()),
                max(20, preview_widget.get_height()),
            )

        def _end_drag(self) -> None:
            if self._drag_start is None:
                return
            index, start_x, start_y = self._drag_start
            x, y = self._drag_delta
            widget = self.layout.widgets[index]
            preview_widget = self.preview_labels[index]
            widget.x, widget.y = self._bounded_preview_position(
                start_x + x,
                start_y + y,
                max(20, preview_widget.get_width()),
                max(20, preview_widget.get_height()),
            )
            self.preview.move(preview_widget, widget.x, widget.y)
            self._mark_layout_dirty()
            self._drag_start = None
            self._clear_interaction_ghost()

        def _begin_resize(self, gesture: Gtk.GestureDrag, index: int) -> None:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            widget = self.layout.widgets[index]
            self._resize_start = (index, widget.size, widget.width, widget.height)
            self._resize_values = (widget.size, widget.width, widget.height)
            preview_widget = self.preview_labels[index]
            self._show_interaction_ghost(
                widget.x,
                widget.y,
                max(20, preview_widget.get_width()),
                max(20, preview_widget.get_height()),
            )
            self.preview.grab_focus()

        def _resize_widget(self, index: int, x: float, y: float) -> None:
            if self._resize_start is None or self._resize_start[0] != index:
                return
            _, start_size, start_width, start_height = self._resize_start
            widget = self.layout.widgets[index]
            size, width, height = resize_values(
                widget, start_size, start_width, start_height, x, y
            )
            if self.grid_snap.get_active() and widget.is_graph:
                width = max(20, snap_to_grid(width, PREVIEW_GRID_SPACING))
                height = max(20, snap_to_grid(height, PREVIEW_GRID_SPACING))
                if widget.is_circular_graph:
                    side = width if abs(x) >= abs(y) else height
                    width = height = side
            if self._resize_values == (size, width, height):
                return
            self._resize_values = (size, width, height)
            preview_widget = self.preview_labels[index]
            if widget.is_graph:
                ghost_width, ghost_height = width, height
            else:
                scale = size / max(1, start_size)
                ghost_width = max(20, round(preview_widget.get_width() * scale))
                ghost_height = max(20, round(preview_widget.get_height() * scale))
            self._show_interaction_ghost(widget.x, widget.y, ghost_width, ghost_height)

        def _end_resize(self) -> None:
            if self._resize_start is None:
                return
            index, _, _, _ = self._resize_start
            self._resize_start = None
            widget = self.layout.widgets[index]
            if self._resize_values is not None:
                widget.size, widget.width, widget.height = self._resize_values
            self._resize_values = None
            self._clear_interaction_ghost()
            self.updating_layout_controls = True
            try:
                self.font_size.set_value(widget.size)
                self.graph_width.set_value(widget.width)
                self.graph_height.set_value(widget.height)
                self.graph_history_seconds.set_value(widget.history_seconds)
            finally:
                self.updating_layout_controls = False
            self._mark_layout_dirty()
            self._refresh_preview()

        def _bounded_preview_position(
            self, x: float, y: float, width: int, height: int
        ) -> tuple[int, int]:
            """Keep an interaction preview and its eventual widget on the canvas."""
            maximum_x = max(0, DISPLAY_SIZE - width)
            maximum_y = max(0, DISPLAY_SIZE - height)
            if self.grid_snap.get_active():
                x, y = (
                    snap_to_grid(x, PREVIEW_GRID_SPACING),
                    snap_to_grid(y, PREVIEW_GRID_SPACING),
                )
            return (
                max(0, min(maximum_x, round(x))),
                max(0, min(maximum_y, round(y))),
            )

        def _show_interaction_ghost(
            self, x: int, y: int, width: int, height: int
        ) -> None:
            if self._interaction_ghost is None:
                self._interaction_ghost = Gtk.Box()
                self._interaction_ghost.set_can_target(False)
                self._interaction_ghost.add_css_class("preview-interaction-ghost")
                self.preview.put(self._interaction_ghost, x, y)
            self._interaction_ghost.set_size_request(width, height)
            self.preview.move(self._interaction_ghost, x, y)

        def _clear_interaction_ghost(self) -> None:
            if self._interaction_ghost is not None:
                self.preview.remove(self._interaction_ghost)
                self._interaction_ghost = None

        def _save_layouts(self) -> None:
            if not self.layout_draft.dirty:
                return
            self.layout_draft.save()
            if self.save_layout_button:
                self.save_layout_button.set_sensitive(False)
            if self.reset_layout_button:
                self.reset_layout_button.set_sensitive(False)
            self._toast("Layout saved")

        def _reset_layouts(self) -> None:
            if not self.layout_draft.dirty:
                return
            self.resetting_layout = True
            try:
                self.layout_draft.reset()
                self.layouts = self.layout_draft.layouts
                self.layout = self.layout_draft.active_layout
                self.layout_selector.set_model(Gtk.StringList.new(list(self.layouts)))
                self.layout_selector.set_selected(
                    list(self.layouts).index(self.layout.name)
                )
            finally:
                self.resetting_layout = False
            self.selected_widget = 0
            if self.save_layout_button:
                self.save_layout_button.set_sensitive(False)
            if self.reset_layout_button:
                self.reset_layout_button.set_sensitive(False)
            self._refresh_preview()
            if self._rebuild_widget_selector(self.selected_widget):
                self._select_widget()
            self._toast("Layout reset to its last saved state")

        def _mark_layout_dirty(self) -> None:
            self.layout_draft.dirty = True
            if self.save_layout_button:
                self.save_layout_button.set_sensitive(True)
            if self.reset_layout_button:
                self.reset_layout_button.set_sensitive(True)

        def _save_telemetry_settings(self) -> None:
            if self.gpu_bdfs:
                selected = self.gpu_selector.get_selected()
                if selected != Gtk.INVALID_LIST_POSITION and selected < len(self.gpu_bdfs):
                    self.settings.gpu_bdf = self.gpu_bdfs[selected]
            self.settings.refresh_interval = self.interval.get_value()
            self.settings.sleep_timeout = int(self.timeout.get_value())
            self.settings.temperature_unit = self._selected_temperature_unit()
            self._save_settings()

        def _save_gif_mask_settings(self) -> None:
            self.settings.gif_mask_mode = ("none", "manual", "openrgb")[
                self.gif_mask_mode.get_selected()
            ]
            rgba = self.gif_mask_color.get_rgba()
            self.settings.gif_mask_color = "#{:02x}{:02x}{:02x}".format(
                round(rgba.red * 255),
                round(rgba.green * 255),
                round(rgba.blue * 255),
            )
            self._save_settings()

        def _gif_mask_color(self) -> str | None:
            mode = self.gif_mask_mode.get_selected()
            if mode == 0:
                return None
            if mode == 1:
                rgba = self.gif_mask_color.get_rgba()
                return "#{:02x}{:02x}{:02x}".format(
                    round(rgba.red * 255),
                    round(rgba.green * 255),
                    round(rgba.blue * 255),
                )
            return argb_v2_3_color()

        def _toggle_gif_sync(self) -> None:
            if not self.gif_sync.get_active():
                self.gif_sync_stop.set()
                return
            if self.gif_mask_mode.get_selected() != 2:
                self.gif_sync.set_active(False)
                self._toast("Select OpenRGB ARGB_V2_3 before enabling GIF sync")
                return
            if self.gif_sync_source is None or self.gif_sync_color is None:
                self.gif_sync.set_active(False)
                self._toast("Upload a GIF with the OpenRGB mask before enabling sync")
                return
            if service_active():
                self.gif_sync.set_active(False)
                self._toast("Stop the background telemetry service before syncing GIF media")
                return
            self.gif_sync_stop.set()
            self.gif_sync_stop = threading.Event()
            threading.Thread(
                target=self._gif_sync_loop,
                args=(self.gif_sync_source, self.gif_sync_color, self.gif_sync_stop),
                daemon=True,
            ).start()

        def _gif_sync_loop(
            self, source: Path, applied_color: str, stop: threading.Event
        ) -> None:
            pending_color: str | None = None
            while not stop.wait(3):
                try:
                    color = argb_v2_3_color()
                    if color == applied_color:
                        pending_color = None
                        continue
                    if color != pending_color:
                        pending_color = color
                        continue
                    if service_active():
                        GLib.idle_add(
                            self._toast,
                            "GIF sync paused while the background telemetry service is running",
                        )
                        continue
                    self.controller.show_animation(source, color)
                    applied_color = color
                    pending_color = None
                    GLib.idle_add(self._toast, "Updated GIF color from OpenRGB")
                except Exception as exc:
                    pending_color = None
                    GLib.idle_add(self._toast, f"GIF sync failed: {exc}")

        def _save_settings(self) -> None:
            try:
                self.settings_store.save(self.settings)
            except OSError as exc:
                self._toast(f"Could not save app settings: {exc}")

        def _choose_file(self, kind: str) -> None:
            dialog = Gtk.FileChooserNative(
                title="Choose media", transient_for=self.window, action=Gtk.FileChooserAction.OPEN
            )
            dialog.connect("response", self._on_file_chosen, kind)
            dialog.show()

        def _on_file_chosen(self, dialog: Gtk.FileChooserNative, response: int, kind: str) -> None:
            try:
                if response == Gtk.ResponseType.ACCEPT and (selected := dialog.get_file()):
                    path = selected.get_path()
                    if path:
                        if kind == "image":
                            self._run(lambda: self.controller.show_image(Path(path)))
                        else:
                            self._run(lambda: self._show_animation(Path(path)))
            finally:
                dialog.destroy()

        def _show_animation(self, source: Path) -> None:
            color = self._gif_mask_color()
            self.controller.show_animation(source, color)
            if color is not None and self.gif_mask_mode.get_selected() == 2:
                self.gif_sync_source = source
                self.gif_sync_color = color
                if self.gif_sync.get_active():
                    GLib.idle_add(self._toggle_gif_sync)

        def _wake(self) -> None:
            self.controller.wake(int(self.timeout.get_value()))

        def _set_timeout(self) -> None:
            self.controller.set_timeout(int(self.timeout.get_value()))

        def _telemetry_once(self) -> None:
            iterator = self.controller.telemetry_updates(
                self._selected_gpu_bdf(),
                self.interval.get_value(),
                lambda: False,
                overlay=True,
                layout=self.layout,
                temperature_unit=self._selected_temperature_unit(),
            )
            next(iterator)

        def _toggle_telemetry(self) -> None:
            if self.telemetry_stop.is_set():
                self.telemetry_stop.clear()
            elif self.telemetry_button and self.telemetry_button.get_label() == "Stop":
                self.telemetry_stop.set()
                self.telemetry_button.set_label("Start")
                self._toast("Telemetry stop requested")
                return
            self.telemetry_button.set_label("Stop")
            self._run(self._telemetry_loop, keep_button=True)

        def _telemetry_loop(self) -> None:
            try:
                for _ in self.controller.telemetry_updates(
                    self._selected_gpu_bdf(),
                    self.interval.get_value(),
                    self.telemetry_stop.is_set,
                    overlay=True,
                    layout=self.layout,
                    temperature_unit=self._selected_temperature_unit(),
                ):
                    pass
            finally:
                GLib.idle_add(self._telemetry_finished)

        def _telemetry_finished(self) -> None:
            if self.telemetry_button:
                self.telemetry_button.set_label("Start")
            self.telemetry_stop.clear()

        def _toggle_background_service(self) -> None:
            if self.service_button and self.service_button.get_label() == "Stop":
                self._run(self._stop_background_service, success_message=None)
            else:
                self.telemetry_stop.set()
                self._run(self._start_background_service, success_message=None)

        def _start_background_service(self) -> None:
            start_service()
            GLib.idle_add(self._set_service_status, True)

        def _stop_background_service(self) -> None:
            stop_service()
            GLib.idle_add(self._set_service_status, False)

        def _refresh_service_status(self) -> None:
            def refresh() -> None:
                active = service_active()
                GLib.idle_add(self._set_service_status, active)

            self._run(refresh, success_message=None)

        def _set_service_status(self, active: bool) -> None:
            if self.service_status_label:
                self.service_status_label.set_text(
                    "Running" if active else "Stopped"
                )
            if self.service_button:
                self.service_button.set_label("Stop" if active else "Start")

        def _selected_gpu_bdf(self) -> str:
            selected = self.gpu_selector.get_selected()
            if selected == Gtk.INVALID_LIST_POSITION or selected >= len(self.gpu_bdfs):
                raise ValueError("no PCI display controller is available for telemetry")
            return self.gpu_bdfs[selected]

        def _selected_temperature_unit(self) -> str:
            return "F" if self.temperature_unit.get_selected() == 1 else "C"

        def _refresh_status(self) -> None:
            def update() -> None:
                status = self.controller.status()
                details = [
                    f"Firmware {status.get('version', {}).get('firmware', 'unknown')}",
                    f"brightness {status.get('brightness', 'unknown')}",
                    f"rotation {status.get('degree', 'unknown')}°",
                    f"timeout {status.get('timeout', 'unknown')}s",
                ]
                GLib.idle_add(self._set_status, " · ".join(details))

            self._run(update, success_message=None)

        def _set_status(self, text: str) -> None:
            if self.status_label:
                self.status_label.set_text(text)

        def _run(self, action: object, *, success_message: str | None = "Completed", keep_button: bool = False) -> None:
            def worker() -> None:
                try:
                    if not callable(action):
                        raise TypeError("internal GUI action was not callable")
                    action()
                except Exception as exc:
                    GLib.idle_add(self._toast, str(exc))
                else:
                    if success_message:
                        GLib.idle_add(self._toast, success_message)

            threading.Thread(target=worker, daemon=True).start()

        def _toast(self, message: str) -> None:
            if self.toast_overlay:
                self.toast_overlay.add_toast(Adw.Toast.new(message))

    SpaceStationApplication().run(None)
