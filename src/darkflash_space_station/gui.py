"""GTK 4 / Libadwaita desktop controller for the Space Station."""

from __future__ import annotations

import subprocess
import shutil
import threading
from pathlib import Path
from uuid import uuid4

from .animation import AnimationCache
from .controller import SpaceStation
from .hid import HidError
from .media import (
    format_temperature,
    render_masked_gif_preview,
    render_telemetry_overlay,
)
from .openrgb import OpenRgbError, adjust_mask_color, argb_v2_3_color
from .layout import (
    DISPLAY_SIZE,
    GRAPH_METRICS,
    GRAPH_STYLES,
    Layout,
    LayoutDraft,
    LayoutMedia,
    LayoutStore,
    WIDGETS,
    resize_values,
    snap_to_grid,
    widget_selector_state,
)
from .service import service_active, start_service, stop_service
from .settings import AppSettings, SettingsStore
from .telemetry import TelemetryHistory


def restored_gif_sync_state(
    store: SettingsStore, settings: AppSettings
) -> tuple[Path | None, str | None]:
    """Restore the retained GIF and its last rendered OpenRGB mask color."""
    source = store.saved_gif()
    if source is None or settings.gif_mask_mode != "openrgb":
        return source, None
    return source, AnimationCache(store).mask_color()
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
        gi.require_version("GdkPixbuf", "2.0")
        gi.require_version("Gtk", "4.0")
        from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk
    except (ImportError, ValueError) as exc:
        raise SystemExit(
            "GTK 4 and Libadwaita bindings are required; install python-gobject and libadwaita"
        ) from exc

    class AnimatedGif(Gtk.DrawingArea):
        def __init__(self, source: Path, size: int) -> None:
            super().__init__()
            self.set_content_width(size)
            self.set_content_height(size)
            self.set_can_target(False)
            self.animation = GdkPixbuf.PixbufAnimation.new_from_file(str(source))
            self.iterator = self.animation.get_iter(None)
            self.was_rooted = False
            self.set_draw_func(self._draw_frame)
            self._schedule_next_frame()

        def _schedule_next_frame(self) -> None:
            delay = max(20, self.iterator.get_delay_time())
            GLib.timeout_add(delay, self._advance_frame)

        def _advance_frame(self) -> bool:
            if self.get_root() is not None:
                self.was_rooted = True
            elif self.was_rooted:
                return GLib.SOURCE_REMOVE
            self.iterator.advance(None)
            self.queue_draw()
            self._schedule_next_frame()
            return GLib.SOURCE_REMOVE

        def _draw_frame(
            self, _area: Gtk.DrawingArea, context: object, width: int, height: int
        ) -> None:
            frame = self.iterator.get_pixbuf()
            scale = max(width / frame.get_width(), height / frame.get_height())
            scaled_width = max(1, round(frame.get_width() * scale))
            scaled_height = max(1, round(frame.get_height() * scale))
            scaled = frame.scale_simple(
                scaled_width, scaled_height, GdkPixbuf.InterpType.BILINEAR
            )
            Gdk.cairo_set_source_pixbuf(
                context,
                scaled,
                (width - scaled_width) / 2,
                (height - scaled_height) / 2,
            )
            context.paint()

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
            self.updating_media_controls = False
            self.resetting_layout = False
            self.telemetry_stop = threading.Event()
            self.layout_apply_lock = threading.Lock()
            self.layout_apply_generation = 0
            self.gif_sync_source, self.gif_sync_color = restored_gif_sync_state(
                self.settings_store, self.settings
            )
            self.openrgb_text_color: str | None = self.gif_sync_color
            self.openrgb_text_error: str | None = None
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
            self.layout_list = Gtk.ListBox(
                selection_mode=Gtk.SelectionMode.SINGLE,
                activate_on_single_click=True,
            )
            self.layout_list.add_css_class("boxed-list")
            self.layout_list_names: list[str] = []
            self._rebuild_layout_list(self.layout.name)
            self.layout_widget_list = Gtk.ListBox(
                selection_mode=Gtk.SelectionMode.SINGLE,
                activate_on_single_click=True,
            )
            self.layout_widget_list.add_css_class("boxed-list")
            self.layout_widget_list.add_css_class("layout-widget-list")
            self.layout_widget_list.connect(
                "row-selected", self._select_layout_widget
            )
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
            self.color_source = Gtk.DropDown.new_from_strings(
                ["Manual color", "OpenRGB ARGB_V2_3"]
            )
            self.text_hue_shift = Gtk.SpinButton.new_with_range(-180, 180, 1)
            self.text_brightness = Gtk.SpinButton.new_with_range(25, 200, 1)
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
            self.gif_hue_shift = Gtk.SpinButton.new_with_range(-180, 180, 1)
            self.gif_hue_shift.set_value(self.settings.gif_hue_shift)
            self.gif_brightness = Gtk.SpinButton.new_with_range(25, 200, 1)
            self.gif_brightness.set_value(self.settings.gif_brightness_percent)
            self.gif_speed_source = Gtk.DropDown.new_from_strings(
                ["Fixed 1×", "CPU load", "GPU load", "Higher of CPU/GPU"]
            )
            self.media_gpu_selector = Gtk.DropDown.new_from_strings(gpu_labels)
            self.media_gpu_selector.set_sensitive(bool(self.gpu_bdfs))
            self.gif_speed_source.set_selected(
                {"none": 0, "cpu": 1, "gpu": 2, "max": 3}[
                    self.settings.gif_speed_source
                ]
            )
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
            self.preview_media = Gtk.Stack()
            self.preview_media.set_size_request(DISPLAY_SIZE, DISPLAY_SIZE)
            self.preview_media.set_can_target(False)
            self.preview_media_picture = Gtk.Picture()
            self.preview_media_picture.set_content_fit(Gtk.ContentFit.COVER)
            self.preview_media_picture.set_can_shrink(True)
            self.preview_media.add_named(
                self.preview_media_picture, "image"
            )
            self.preview_media_gif: AnimatedGif | None = None
            self.preview_media_gif_path: Path | None = None
            self.preview_media.set_visible(False)
            self.preview.put(self.preview_media, 0, 0)
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
            self.window.set_default_size(1440, 800)
            font_keys = Gtk.EventControllerKey()
            font_keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            font_keys.connect("key-pressed", self._preview_font_key)
            self.window.add_controller(font_keys)
            self.toast_overlay = Adw.ToastOverlay()
            toolbar = Adw.ToolbarView()
            header = Adw.HeaderBar()
            sections = self._section_stack()
            switcher = Adw.ViewSwitcher()
            switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
            switcher.set_stack(sections)
            header.set_title_widget(switcher)
            toolbar.add_top_bar(header)
            toolbar.set_content(sections)
            self.toast_overlay.set_child(toolbar)
            self.window.set_content(self.toast_overlay)
            self._install_preview_styles()
            self.window.present()
            self._refresh_status()
            self._refresh_service_status()
            GLib.timeout_add_seconds(3, self._refresh_openrgb_text_color)

        def do_shutdown(self) -> None:
            self.telemetry_stop.set()
            super().do_shutdown()

        @staticmethod
        def _preferences_page(
            *groups: Adw.PreferencesGroup,
        ) -> Adw.PreferencesPage:
            page = Adw.PreferencesPage()
            for group in groups:
                page.add(group)
            return page

        def _section_stack(self) -> Adw.ViewStack:
            stack = Adw.ViewStack()
            stack.add_titled(
                self._preferences_page(self._status_group(), self._display_group()),
                "device",
                "Device",
            )
            stack.add_titled(
                self._preferences_page(self._telemetry_group()),
                "telemetry",
                "Telemetry",
            )
            stack.add_titled(self._layout_page(), "layout", "Layout")
            stack.set_visible_child_name("layout")
            return stack

        def _layout_page(self) -> Gtk.Paned:
            preview_group, layouts_group, controls_group = self._layout_groups()
            preview_page = self._preferences_page(preview_group)
            self.widget_controls_page = self._preferences_page(controls_group)
            self.media_controls_page = self._preferences_page(self._media_group())
            self.context_controls = Gtk.Stack()
            self.context_controls.add_named(
                self.widget_controls_page, "widget"
            )
            self.context_controls.add_named(self.media_controls_page, "media")
            layouts_page = self._preferences_page(layouts_group)
            self.widgets_page = self._preferences_page(
                self._layout_widgets_group()
            )
            self.widgets_page.set_visible(False)
            widget_split = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
            widget_split.set_position(280)
            widget_split.set_wide_handle(True)
            widget_split.set_shrink_start_child(False)
            widget_split.set_shrink_end_child(False)
            widget_split.set_resize_start_child(False)
            widget_split.set_resize_end_child(True)
            widget_split.set_start_child(self.widgets_page)
            widget_split.set_end_child(preview_page)
            workspace_split = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
            workspace_split.set_position(280)
            workspace_split.set_wide_handle(True)
            workspace_split.set_shrink_start_child(False)
            workspace_split.set_shrink_end_child(False)
            workspace_split.set_resize_start_child(False)
            workspace_split.set_resize_end_child(True)
            workspace_split.set_start_child(layouts_page)
            workspace_split.set_end_child(widget_split)
            controls_split = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
            controls_split.set_position(1320)
            controls_split.set_wide_handle(True)
            controls_split.set_shrink_start_child(False)
            controls_split.set_shrink_end_child(True)
            controls_split.set_resize_start_child(True)
            controls_split.set_resize_end_child(False)
            controls_split.set_start_child(workspace_split)
            self.context_controls.set_size_request(120, -1)
            controls_split.set_end_child(self.context_controls)
            self.context_controls.set_visible(self.selected_widget >= 0)
            return controls_split

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
                title="Layout media",
                description="Choose the background shown beneath this layout. Changes stay in preview until applied.",
            )
            image = Adw.ActionRow(title="Background image", subtitle="Center-crop and display as a static video background")
            button = Gtk.Button(label="Choose image", valign=Gtk.Align.CENTER)
            button.connect("clicked", lambda *_: self._choose_file("image"))
            image.add_suffix(button)
            group.add(image)
            animation = Adw.ActionRow(title="Animated GIF", subtitle="Convert and display as a looping video background")
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
            hue = Adw.ActionRow(
                title="Mask hue shift",
                subtitle="Rotate the display mask hue from −180° to +180°.",
            )
            self.gif_hue_shift.connect(
                "value-changed", lambda *_: self._save_gif_mask_settings()
            )
            hue.add_suffix(self.gif_hue_shift)
            group.add(hue)
            brightness = Adw.ActionRow(
                title="Mask brightness",
                subtitle="Scale display-mask brightness from 25% to 200%.",
            )
            self.gif_brightness.connect(
                "value-changed", lambda *_: self._save_gif_mask_settings()
            )
            brightness.add_suffix(self.gif_brightness)
            group.add(brightness)
            speed = Adw.ActionRow(
                title="GIF speed",
                subtitle="Use ten cached speeds from 1× at 0–10% load to 3× at 91–100%.",
            )
            self.gif_speed_source.connect(
                "notify::selected", lambda *_: self._save_gif_mask_settings()
            )
            speed.add_suffix(self.gif_speed_source)
            group.add(speed)
            animation_gpu = Adw.ActionRow(
                title="Animation GPU",
                subtitle="GPU used when the speed source is GPU load.",
            )
            animation_gpu.add_suffix(self.media_gpu_selector)
            group.add(animation_gpu)
            self.animation_gpu_row = animation_gpu
            self.media_gpu_selector.connect(
                "notify::selected", lambda *_: self._save_gif_mask_settings()
            )
            sync = Adw.ActionRow(title="Selected media")
            self.gif_sync_status_label = Gtk.Label(
                label=(
                    "Saved GIF ready"
                    if self.gif_sync_source is not None
                    else "Select a GIF"
                )
            )
            sync.add_suffix(self.gif_sync_status_label)
            group.add(sync)
            clear = Adw.ActionRow(title="Remove layout media")
            button = Gtk.Button(label="Remove", valign=Gtk.Align.CENTER)
            button.connect("clicked", lambda *_: self._remove_layout_media())
            clear.add_suffix(button)
            group.add(clear)
            apply_media = Adw.ActionRow(
                title="Apply to display",
                subtitle="Save this layout and restart the Display service with its media.",
            )
            button = Gtk.Button(label="Apply", valign=Gtk.Align.CENTER)
            button.add_css_class("suggested-action")
            button.connect("clicked", lambda *_: self._apply_layout_media())
            apply_media.add_suffix(button)
            group.add(apply_media)
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
                title="Display runtime",
                description="Keep the display service running to prevent sleep and manage the background.",
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
            row = Adw.ActionRow(
                title="Temporary telemetry preview",
                subtitle="Preview the current in-app layout without changing the saved service layout.",
            )
            once = Gtk.Button(label="Send once", valign=Gtk.Align.CENTER)
            once.connect("clicked", lambda *_: self._run(self._telemetry_once))
            row.add_suffix(once)
            self.telemetry_button = Gtk.Button(label="Start", valign=Gtk.Align.CENTER)
            self.telemetry_button.connect("clicked", lambda *_: self._toggle_telemetry())
            row.add_suffix(self.telemetry_button)
            group.add(row)
            service = Adw.ActionRow(
                title="Display service",
                subtitle="Keep enabled: prevents sleep, restores GIF media, updates adaptive playback, and renders the saved layout.",
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

        def _layout_groups(
            self,
        ) -> tuple[
            Adw.PreferencesGroup,
            Adw.PreferencesGroup,
            Adw.PreferencesGroup,
        ]:
            preview_group = Adw.PreferencesGroup(title="Telemetry layout", description="Drag widgets to position them; drag a selected widget's lower-right handle to resize it. Changes remain in memory until saved.")
            layouts_group = Adw.PreferencesGroup(title="Layouts")
            layout_header = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL,
                spacing=12,
                margin_bottom=6,
            )
            new = Gtk.Button(label="New layout")
            new.connect("clicked", lambda *_: self._new_layout())
            new.set_halign(Gtk.Align.START)
            layout_header.append(new)
            layouts_group.add(layout_header)
            layout_scroller = Gtk.ScrolledWindow(
                hscrollbar_policy=Gtk.PolicyType.NEVER,
                vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
                propagate_natural_height=True,
                min_content_height=200,
                max_content_height=520,
            )
            layout_scroller.set_child(self.layout_list)
            layouts_group.add(layout_scroller)
            self.layout_list.connect("row-selected", self._select_layout)

            preview_box = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL,
                halign=Gtk.Align.CENTER,
            )
            preview_box.append(self.preview)
            preview_group.add(preview_box)
            grid_snap = Adw.ActionRow(
                title="Snap to grid",
                subtitle=f"Snap placement and graph resizing to {PREVIEW_GRID_SPACING}-pixel grid lines.",
            )
            self.grid_snap.connect("notify::active", lambda *_: self._save_grid_snap())
            grid_snap.add_suffix(self.grid_snap)
            preview_group.add(grid_snap)
            action_bar = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL,
                spacing=8,
                margin_top=6,
            )
            media = Gtk.Button(label="Media")
            media.connect("clicked", lambda *_: self._show_media_controls())
            action_bar.append(media)
            self.widgets_button = Gtk.ToggleButton(label="Widgets", active=False)
            self.widgets_button.connect(
                "toggled", lambda *_: self._toggle_widget_list()
            )
            action_bar.append(self.widgets_button)
            action_spacer = Gtk.Box()
            action_spacer.set_hexpand(True)
            action_bar.append(action_spacer)
            self.reset_layout_button = Gtk.Button(label="Cancel changes")
            self.reset_layout_button.set_sensitive(False)
            self.reset_layout_button.connect("clicked", lambda *_: self._reset_layouts())
            action_bar.append(self.reset_layout_button)
            self.save_layout_button = Gtk.Button(label="Save")
            self.save_layout_button.add_css_class("suggested-action")
            self.save_layout_button.set_sensitive(False)
            self.save_layout_button.connect("clicked", lambda *_: self._save_layouts())
            action_bar.append(self.save_layout_button)
            preview_group.add(action_bar)
            controls_group = Adw.PreferencesGroup(
                title="Widget controls",
                description="Style the widget selected in the adjacent layout-widget list.",
            )
            self._rebuild_widget_selector(self.selected_widget)
            self.widget_selector.connect("notify::selected", lambda *_: self._select_widget())
            gpu = Adw.ActionRow(
                title="GPU",
                subtitle="Telemetry source for this GPU widget",
            )
            gpu.add_suffix(self.gpu_selector)
            controls_group.add(gpu)
            self.gpu_row = gpu
            temperature_unit = Adw.ActionRow(
                title="Temperature unit",
                subtitle="Unit for this temperature widget",
            )
            temperature_unit.add_suffix(self.temperature_unit)
            controls_group.add(temperature_unit)
            self.temperature_unit_row = temperature_unit
            self.gpu_selector.connect(
                "notify::selected", lambda *_: self._style_widget()
            )
            self.temperature_unit.connect(
                "notify::selected", lambda *_: self._style_widget()
            )
            font = Adw.ActionRow(title="Font")
            font.add_suffix(self.font_selector)
            controls_group.add(font)
            self.font_row = font
            size = Adw.ActionRow(title="Font size")
            size.add_suffix(self.font_size)
            controls_group.add(size)
            self.font_size_row = size
            width = Adw.ActionRow(title="Graph width")
            width.add_suffix(self.graph_width)
            controls_group.add(width)
            self.graph_width_row = width
            height = Adw.ActionRow(title="Graph height")
            height.add_suffix(self.graph_height)
            controls_group.add(height)
            self.graph_height_row = height
            history = Adw.ActionRow(
                title="Graph history",
                subtitle="Rolling window in seconds",
            )
            history.add_suffix(self.graph_history_seconds)
            controls_group.add(history)
            self.graph_history_row = history
            color = Adw.ActionRow(title="Color")
            color.add_suffix(self.color_source)
            color.add_suffix(self.color)
            controls_group.add(color)
            self.color_row = color
            text_hue = Adw.ActionRow(
                title="OpenRGB hue shift",
                subtitle="Rotate the matched text hue from −180° to +180°.",
            )
            text_hue.add_suffix(self.text_hue_shift)
            controls_group.add(text_hue)
            self.text_hue_row = text_hue
            text_brightness = Adw.ActionRow(
                title="OpenRGB brightness",
                subtitle="Scale the matched text brightness from 25% to 200%.",
            )
            text_brightness.add_suffix(self.text_brightness)
            controls_group.add(text_brightness)
            self.text_brightness_row = text_brightness
            text = Adw.ActionRow(
                title="Custom text",
                subtitle="Used only by Custom text widgets",
            )
            text.add_suffix(self.custom_text)
            controls_group.add(text)
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
            self.color_source.connect(
                "notify::selected", lambda *_: self._style_widget()
            )
            self.text_hue_shift.connect(
                "value-changed", lambda *_: self._style_widget()
            )
            self.text_brightness.connect(
                "value-changed", lambda *_: self._style_widget()
            )
            self.color.connect("notify::rgba", lambda *_: self._style_widget())
            self.custom_text.connect("changed", lambda *_: self._style_widget())
            self._refresh_preview()
            self._select_widget()
            return preview_group, layouts_group, controls_group

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

        def _rebuild_layout_list(self, selected_name: str) -> None:
            while row := self.layout_list.get_row_at_index(0):
                self.layout_list.remove(row)
            self.layout_list_names = list(self.layouts)
            for name in self.layout_list_names:
                row = Gtk.ListBoxRow()
                content = Gtk.Box(
                    orientation=Gtk.Orientation.HORIZONTAL,
                    spacing=12,
                    margin_start=8,
                    margin_end=8,
                    margin_top=8,
                    margin_bottom=8,
                )
                content.append(self._layout_thumbnail(self.layouts[name]))
                label = Gtk.Label(
                    label=name,
                    xalign=0,
                    hexpand=True,
                    valign=Gtk.Align.CENTER,
                )
                content.append(label)
                row.set_child(content)
                self.layout_list.append(row)
            if selected_name in self.layout_list_names:
                self.layout_list.select_row(
                    self.layout_list.get_row_at_index(
                        self.layout_list_names.index(selected_name)
                    )
                )

        def _layout_thumbnail(self, layout: Layout) -> Gtk.Widget:
            sample = {
                "cpu": {"temperature": 42, "load": 37},
                "gpu": {"temperature": 51, "load": 64},
                "network": {"upload": 2.4, "download": 15.8},
            }
            history = TelemetryHistory()
            history.add(sample)
            try:
                image = render_telemetry_overlay(
                    sample,
                    layout,
                    history,
                    self.settings.temperature_unit,
                    self.openrgb_text_color or "#ffffff",
                )
                texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(image))
                picture = Gtk.Picture.new_for_paintable(texture)
                picture.set_content_fit(Gtk.ContentFit.CONTAIN)
                picture.set_size_request(104, 104)
                picture.set_can_shrink(True)
                picture.set_can_target(False)
                thumbnail = Gtk.Overlay()
                thumbnail.set_size_request(104, 104)
                thumbnail.set_overflow(Gtk.Overflow.HIDDEN)
                thumbnail.add_css_class("layout-thumbnail")
                thumbnail.set_child(self._layout_thumbnail_media(layout))
                thumbnail.add_overlay(picture)
                return thumbnail
            except (HidError, OSError, OpenRgbError, ValueError) as exc:
                unavailable = Gtk.Label(
                    label="Preview\nunavailable",
                    justify=Gtk.Justification.CENTER,
                )
                unavailable.set_tooltip_text(str(exc))
                unavailable.set_size_request(104, 104)
                unavailable.add_css_class("layout-thumbnail")
                return unavailable

        def _layout_thumbnail_media(self, layout: Layout) -> Gtk.Widget:
            media = self._effective_layout_media(layout)
            source = self._preview_media_source(media)
            if source is None or not source.is_file():
                return Gtk.Box()
            if media.kind == "gif":
                return AnimatedGif(source, 104)
            if media.kind == "image":
                picture = Gtk.Picture.new_for_filename(str(source))
                picture.set_content_fit(Gtk.ContentFit.COVER)
                picture.set_can_shrink(True)
                picture.set_can_target(False)
                return picture
            return Gtk.Box()

        def _preview_media_source(self, media: LayoutMedia) -> Path | None:
            source = Path(media.file) if media.file else None
            if (
                media.kind != "gif"
                or source is None
                or media.gif_mask_mode == "none"
            ):
                return source
            if media.gif_mask_mode == "manual":
                color = media.gif_mask_color
            elif media.gif_mask_mode == "openrgb":
                color = self.openrgb_text_color or argb_v2_3_color()
                self.openrgb_text_color = color
            else:
                raise ValueError(
                    f"unsupported GIF mask mode: {media.gif_mask_mode}"
                )
            adjusted = adjust_mask_color(
                color,
                media.gif_hue_shift,
                media.gif_brightness_percent,
            )
            return render_masked_gif_preview(
                source,
                adjusted,
                self.settings_store.path.parent / "media" / "previews",
            )

        def _refresh_layout_thumbnails(self) -> None:
            self.resetting_layout = True
            try:
                self._rebuild_layout_list(self.layout.name)
            finally:
                self.resetting_layout = False

        def _select_layout(
            self, _layout_list: Gtk.ListBox, row: Gtk.ListBoxRow | None
        ) -> None:
            if self.resetting_layout or row is None:
                return
            selected = row.get_index()
            if selected < 0 or selected >= len(self.layout_list_names):
                return
            name = self.layout_list_names[selected]
            changed = name != self.layout.name
            if changed and self.layout_draft.dirty:
                self._toast("Unsaved layout changes remain in memory; save to write them to disk.")
            self.layout_draft.select(name, mark_dirty=False)
            self.layout = self.layout_draft.active_layout
            self.selected_widget = -1
            self._rebuild_widget_selector(self.selected_widget)
            self._set_widget_controls_visible(False)
            self._refresh_preview()
            self.settings.selected_layout = name
            self._save_settings()
            if changed:
                self._apply_selected_layout(name)

        def _apply_selected_layout(self, name: str) -> None:
            self.telemetry_stop.set()
            if self.telemetry_button:
                self.telemetry_button.set_label("Start")
            self.layout_apply_generation += 1
            generation = self.layout_apply_generation

            def apply() -> None:
                with self.layout_apply_lock:
                    if generation != self.layout_apply_generation:
                        return
                    self.layout_store.save_active(name)
                    stop_service()
                    start_service()
                    GLib.idle_add(self._set_service_status, True)
                    GLib.idle_add(
                        self._toast, f"{name} applied to the display"
                    )

            self._run(apply, success_message=None)

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
            self._rebuild_layout_list(name)
            self.settings.selected_layout = name
            self._save_settings()
            self._mark_layout_dirty()

        def _layout_widgets_group(self) -> Adw.PreferencesGroup:
            group = Adw.PreferencesGroup(
                title="Layout widgets",
                description="Widgets used by the selected layout.",
            )
            add = Gtk.MenuButton(label="Add widget", halign=Gtk.Align.START)
            add.set_popover(self._add_widget_popover())
            group.add(add)
            scroller = Gtk.ScrolledWindow(
                hscrollbar_policy=Gtk.PolicyType.NEVER,
                vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
                min_content_height=300,
                max_content_height=620,
            )
            scroller.set_vexpand(True)
            scroller.set_child(self.layout_widget_list)
            group.add(scroller)
            return group

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
            catalog = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)

            def add_section(title: str, items: list[tuple[str, object]]) -> None:
                heading = Gtk.Label(
                    label=title,
                    xalign=0,
                    margin_start=8,
                    margin_top=12,
                    margin_bottom=4,
                )
                heading.add_css_class("heading")
                catalog.append(heading)
                for label, callback in items:
                    button = Gtk.Button(
                        label=label,
                        halign=Gtk.Align.FILL,
                        hexpand=True,
                    )
                    button.add_css_class("flat")
                    button.connect(
                        "clicked",
                        lambda _button, callback=callback: (
                            callback(),
                            popover.popdown(),
                        ),
                    )
                    catalog.append(button)

            add_section(
                "Text",
                [
                    (
                        labels[metric],
                        lambda metric=metric: self._add_widget(metric),
                    )
                    for metric in WIDGETS
                ],
            )
            for style in GRAPH_STYLES:
                add_section(
                    style.replace("-", " ").title(),
                    [
                        (
                            labels[metric],
                            lambda metric=metric, style=style: self._add_widget(
                                metric, graph_style=style
                            ),
                        )
                        for metric in GRAPH_METRICS
                    ],
                )
            scroller = Gtk.ScrolledWindow(
                hscrollbar_policy=Gtk.PolicyType.NEVER,
                vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
                min_content_width=280,
                min_content_height=320,
                max_content_height=560,
            )
            scroller.set_child(catalog)
            popover.set_child(scroller)
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
            self._set_widget_controls_visible(True)
            widget = self.layout.widgets[self.selected_widget]
            self.preview_font = None
            self.updating_layout_controls = True
            try:
                self.layout_widget_list.select_row(
                    self.layout_widget_list.get_row_at_index(self.selected_widget)
                )
                selected_gpu_bdf = widget.gpu_bdf or self.settings.gpu_bdf
                self.gpu_selector.set_selected(
                    self.gpu_bdfs.index(selected_gpu_bdf)
                    if selected_gpu_bdf in self.gpu_bdfs
                    else Gtk.INVALID_LIST_POSITION
                )
                self.temperature_unit.set_selected(
                    1
                    if (widget.temperature_unit or self.settings.temperature_unit)
                    == "F"
                    else 0
                )
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
                self.color_source.set_selected(
                    1 if widget.color_source == "openrgb" else 0
                )
                self.text_hue_shift.set_value(widget.color_hue_shift)
                self.text_brightness.set_value(
                    widget.color_brightness_percent
                )
                self.custom_text.set_text(widget.text)
            finally:
                self.updating_layout_controls = False
            self._update_editor_visibility(widget)
            self._refresh_preview()

        def _select_layout_widget(
            self, _widget_list: Gtk.ListBox, row: Gtk.ListBoxRow | None
        ) -> None:
            if self.updating_layout_controls or row is None:
                return
            index = row.get_index()
            if 0 <= index < len(self.layout.widgets):
                self.selected_widget = index
                self.widget_selector.set_selected(index)
                self._select_widget()

        def _rebuild_layout_widget_list(
            self, labels: list[str], selected: int
        ) -> None:
            while row := self.layout_widget_list.get_row_at_index(0):
                self.layout_widget_list.remove(row)
            for label_text in labels:
                row = Gtk.ListBoxRow()
                label = Gtk.Label(
                    label=label_text,
                    xalign=0,
                    margin_start=12,
                    margin_end=12,
                    margin_top=10,
                    margin_bottom=10,
                )
                row.set_child(label)
                self.layout_widget_list.append(row)
            if labels and selected >= 0:
                self.layout_widget_list.select_row(
                    self.layout_widget_list.get_row_at_index(selected)
                )

        def _set_widget_controls_visible(self, visible: bool) -> None:
            if hasattr(self, "context_controls"):
                if visible:
                    self.context_controls.set_visible_child_name("widget")
                self.context_controls.set_visible(visible)

        def _show_media_controls(self) -> None:
            self._load_layout_media_controls()
            self.context_controls.set_visible_child_name("media")
            self.context_controls.set_visible(True)

        def _toggle_widget_list(self) -> None:
            if hasattr(self, "widgets_page"):
                self.widgets_page.set_visible(
                    self.widgets_button.get_active()
                )

        def _update_editor_visibility(self, widget: Widget) -> None:
            is_graph = widget.is_graph
            is_custom_text = widget.metric == "custom-text"
            self.gpu_row.set_visible(widget.metric.startswith("gpu-"))
            self.temperature_unit_row.set_visible(
                widget.metric.endswith("temperature")
            )
            self.font_row.set_visible(not is_graph)
            self.font_size_row.set_visible(not is_graph)
            self.graph_width_row.set_visible(is_graph)
            self.graph_height_row.set_visible(is_graph)
            self.graph_history_row.set_visible(is_graph)
            self.color_source.set_visible(not is_graph)
            self.color_row.set_visible(True)
            matches_openrgb = (
                not is_graph and self.color_source.get_selected() == 1
            )
            self.text_hue_row.set_visible(matches_openrgb)
            self.text_brightness_row.set_visible(matches_openrgb)
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
            if not widget.is_graph:
                widget.color_source = (
                    "openrgb" if self.color_source.get_selected() == 1 else "manual"
                )
                widget.color_hue_shift = self.text_hue_shift.get_value()
                widget.color_brightness_percent = (
                    self.text_brightness.get_value()
                )
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
            selected_gpu = self.gpu_selector.get_selected()
            if widget.metric.startswith("gpu-"):
                widget.gpu_bdf = (
                    self.gpu_bdfs[selected_gpu]
                    if selected_gpu != Gtk.INVALID_LIST_POSITION
                    and selected_gpu < len(self.gpu_bdfs)
                    else ""
                )
            if widget.metric.endswith("temperature"):
                widget.temperature_unit = self._selected_temperature_unit()
            self._update_editor_visibility(widget)
            self._mark_layout_dirty()
            self._refresh_preview()

        def _refresh_preview(self) -> None:
            self._clear_interaction_ghost()
            self._refresh_media_preview()
            for label in self.preview_labels:
                self.preview.remove(label)
            self.preview_labels = []
            self.preview_values = []
            samples = {
                "cpu-temperature": (
                    "CPU Temperature",
                    "",
                ),
                "cpu-load": ("CPU Load", "17%"),
                "gpu-temperature": (
                    "GPU Temperature",
                    "",
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
                if widget.metric.endswith("temperature"):
                    sample = format_temperature(
                        42 if widget.metric.startswith("cpu-") else 50,
                        widget.temperature_unit or self.settings.temperature_unit,
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
                    preview_color = (
                        adjust_mask_color(
                            self.openrgb_text_color,
                            widget.color_hue_shift,
                            widget.color_brightness_percent,
                        )
                        if widget.color_source == "openrgb"
                        and self.openrgb_text_color is not None
                        else widget.color
                    )
                    value.set_markup(
                        f'<span foreground="{preview_color}" font_desc="{font} {widget.size}">{sample}</span>'
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

        def _refresh_media_preview(self) -> None:
            media = self._effective_layout_media()
            try:
                source = self._preview_media_source(media)
            except Exception as exc:
                self.preview_media.set_visible(False)
                self._toast(str(exc))
                return
            if (
                media.kind in {"image", "gif"}
                and source is not None
                and source.is_file()
            ):
                if media.kind == "gif":
                    if source != self.preview_media_gif_path:
                        if self.preview_media_gif is not None:
                            self.preview_media.remove(self.preview_media_gif)
                        self.preview_media_gif = AnimatedGif(
                            source, DISPLAY_SIZE
                        )
                        self.preview_media.add_named(
                            self.preview_media_gif, "gif"
                        )
                        self.preview_media_gif_path = source
                    self.preview_media.set_visible_child_name("gif")
                else:
                    self.preview_media_picture.set_filename(str(source))
                    self.preview_media.set_visible_child_name("image")
                self.preview_media.set_visible(True)
            else:
                self.preview_media.set_visible(False)

        def _refresh_openrgb_text_color(self) -> bool:
            uses_text_color = any(
                not widget.is_graph and widget.color_source == "openrgb"
                for widget in self.layout.widgets
            )
            uses_media_mask = any(
                self._effective_layout_media(layout).gif_mask_mode == "openrgb"
                and self._effective_layout_media(layout).kind == "gif"
                for layout in self.layouts.values()
            )
            if not uses_text_color and not uses_media_mask:
                return True
            try:
                color = argb_v2_3_color()
            except OpenRgbError as exc:
                message = str(exc)
                if message != self.openrgb_text_error:
                    self.openrgb_text_error = message
                    self._toast(message)
                return True
            self.openrgb_text_error = None
            if color != self.openrgb_text_color:
                self.openrgb_text_color = color
                self._refresh_preview()
                self._refresh_layout_thumbnails()
            return True

        def _rebuild_widget_selector(self, selected: int | None = None) -> bool:
            requested = self.selected_widget if selected is None else selected
            labels, selected_widget = widget_selector_state(
                self.layout.widgets,
                requested,
            )
            if requested < 0:
                selected_widget = -1
            self.updating_layout_controls = True
            try:
                self.widget_selector.set_model(Gtk.StringList.new(labels))
                if labels and selected_widget >= 0:
                    self.selected_widget = selected_widget
                    self.widget_selector.set_selected(self.selected_widget)
                else:
                    self.selected_widget = -1
                    self.widget_selector.set_selected(Gtk.INVALID_LIST_POSITION)
                self._rebuild_layout_widget_list(labels, self.selected_widget)
            finally:
                self.updating_layout_controls = False
            self._set_widget_controls_visible(self.selected_widget >= 0)
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
                "background-color: alpha(#1e1e1e, 0.35); }"
                ".layout-thumbnail { background-color: #101010; border: 1px solid alpha(#ffffff, 0.15); "
                "border-radius: 8px; padding: 2px; }"
                ".layout-widget-list row:selected { background-color: alpha(#3584e4, 0.28); }"
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
            self._rebuild_layout_list(self.layout.name)
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
                self._rebuild_layout_list(self.layout.name)
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
            self.settings.refresh_interval = self.interval.get_value()
            self.settings.sleep_timeout = int(self.timeout.get_value())
            self._save_settings()

        def _effective_layout_media(
            self, layout: Layout | None = None
        ) -> LayoutMedia:
            media = (layout or self.layout).media
            if media.kind != "inherit":
                return media
            retained = self.settings_store.saved_gif()
            return LayoutMedia(
                kind="gif" if retained else "none",
                file=str(retained) if retained else "",
                gif_mask_mode=self.settings.gif_mask_mode,
                gif_mask_color=self.settings.gif_mask_color,
                gif_speed_source=self.settings.gif_speed_source,
                gif_speed_gpu_bdf=self.settings.gpu_bdf,
                gif_hue_shift=self.settings.gif_hue_shift,
                gif_brightness_percent=self.settings.gif_brightness_percent,
            )

        def _materialize_layout_media(self) -> LayoutMedia:
            if self.layout.media.kind == "inherit":
                self.layout.media = self._effective_layout_media()
            return self.layout.media

        def _load_layout_media_controls(self) -> None:
            media = self._effective_layout_media()
            self.updating_media_controls = True
            self.gif_mask_mode.set_selected(
                ("none", "manual", "openrgb").index(media.gif_mask_mode)
            )
            rgba = Gdk.RGBA()
            if rgba.parse(media.gif_mask_color):
                self.gif_mask_color.set_rgba(rgba)
            self.gif_speed_source.set_selected(
                ("none", "cpu", "gpu", "max").index(media.gif_speed_source)
            )
            selected_gpu = media.gif_speed_gpu_bdf or self.settings.gpu_bdf
            self.media_gpu_selector.set_selected(
                self.gpu_bdfs.index(selected_gpu)
                if selected_gpu in self.gpu_bdfs
                else Gtk.INVALID_LIST_POSITION
            )
            self.animation_gpu_row.set_visible(
                media.gif_speed_source == "gpu"
            )
            self.gif_hue_shift.set_value(media.gif_hue_shift)
            self.gif_brightness.set_value(media.gif_brightness_percent)
            self.updating_media_controls = False
            selected = Path(media.file).name if media.file else "None"
            self.gif_sync_status_label.set_text(
                f"{media.kind.upper()}: {selected}"
                if media.kind in {"image", "gif"} and media.file
                else "None"
            )

        def _save_gif_mask_settings(self) -> None:
            if self.updating_media_controls:
                return
            media = self._materialize_layout_media()
            media.gif_mask_mode = ("none", "manual", "openrgb")[
                self.gif_mask_mode.get_selected()
            ]
            rgba = self.gif_mask_color.get_rgba()
            media.gif_mask_color = "#{:02x}{:02x}{:02x}".format(
                round(rgba.red * 255),
                round(rgba.green * 255),
                round(rgba.blue * 255),
            )
            media.gif_speed_source = ("none", "cpu", "gpu", "max")[
                self.gif_speed_source.get_selected()
            ]
            selected_gpu = self.media_gpu_selector.get_selected()
            media.gif_speed_gpu_bdf = (
                self.gpu_bdfs[selected_gpu]
                if selected_gpu != Gtk.INVALID_LIST_POSITION
                and selected_gpu < len(self.gpu_bdfs)
                else ""
            )
            self.animation_gpu_row.set_visible(
                media.gif_speed_source == "gpu"
            )
            media.gif_hue_shift = self.gif_hue_shift.get_value()
            media.gif_brightness_percent = self.gif_brightness.get_value()
            self._refresh_preview()
            self._refresh_layout_thumbnails()
            self._mark_layout_dirty()

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
                        self._set_layout_media(Path(path), kind)
            finally:
                dialog.destroy()

        def _set_layout_media(self, source: Path, kind: str) -> None:
            media_dir = self.settings_store.path.parent / "media" / "layouts"
            media_dir.mkdir(parents=True, exist_ok=True)
            target = media_dir / f"{uuid4().hex}{source.suffix.lower()}"
            shutil.copy2(source, target)
            media = self._materialize_layout_media()
            media.kind = kind
            media.file = str(target)
            self._save_gif_mask_settings()
            self._load_layout_media_controls()
            self._refresh_preview()
            self._refresh_layout_thumbnails()
            self._mark_layout_dirty()

        def _remove_layout_media(self) -> None:
            media = self._materialize_layout_media()
            media.kind = "none"
            media.file = ""
            self._load_layout_media_controls()
            self._refresh_preview()
            self._refresh_layout_thumbnails()
            self._mark_layout_dirty()

        def _apply_layout_media(self) -> None:
            self._save_gif_mask_settings()
            self._save_layouts()

            def apply() -> None:
                stop_service()
                start_service()

            self._run(apply, "Layout media applied")

        def _wake(self) -> None:
            self.controller.wake(int(self.timeout.get_value()))

        def _set_timeout(self) -> None:
            self.controller.set_timeout(int(self.timeout.get_value()))

        def _telemetry_once(self) -> None:
            iterator = self.controller.telemetry_updates(
                self.settings.gpu_bdf,
                self.interval.get_value(),
                lambda: False,
                overlay=True,
                layout=self.layout,
                temperature_unit=self.settings.temperature_unit,
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
                    self.settings.gpu_bdf,
                    self.interval.get_value(),
                    self.telemetry_stop.is_set,
                    overlay=True,
                    layout=self.layout,
                    temperature_unit=self.settings.temperature_unit,
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

    Gtk.init()
    SpaceStationApplication().run(None)
