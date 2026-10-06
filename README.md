# DarkFlash Space Station for Linux

Native Linux control for the 320×320 darkFlash Space Station cooler display
(`darkFlash Inc.`, USB `1d6b:0102`). It replaces the display-control portion of
DF Space Station with a command-line tool and a GTK 4 / Libadwaita desktop app.

This is an independent community project based on behavior observed from the
hardware and vendor application. It is not affiliated with or endorsed by
DarkFlash.

> **One writer only:** do not run this project and the Windows DF Space Station
> application at the same time. The display accepts only one active HID writer.

## Features

- Device status, wake/resume, and the observed sleep-timeout command.
- Center-cropped still images as foreground `.osd` media.
- GIFs as 320×320 H.264 MP4 background media, decoded and looped by the cooler
  at 20 FPS.
- Transparent foreground OSD uploads that clear a previous static image and
  expose the video background.
- Native CPU/GPU stock-theme telemetry, including a 5-second rolling average
  for CPU package temperature.
- A visual telemetry-layout editor with text widgets and six graph styles.
- Optional GIF color masking and synchronization with an OpenRGB LED.
- A managed user service for a persistent telemetry overlay.
- A local HTTP API for Home Assistant automations that select layouts and read
  telemetry.

The firmware has separate background-video and foreground-OSD layers. A GIF
upload always sends the MP4 background followed by a transparent OSD. A still
image is an OSD upload and will cover a video until `media clear-overlay` runs.

Brightness and rotation are reported by `status` but intentionally remain
read-only: their firmware write commands have not been captured yet, so this
project does not guess at HID operations.

## Requirements

- Python 3.11+
- ImageMagick (`magick`/`convert`) and Fontconfig (`fc-list`) for media, masked
  previews, and overlays
- FFmpeg with `libx264` for GIF conversion
- `lm_sensors` (`sensors -j`) for CPU temperatures
- `nvtop` for GPU utilization and GPU-driven adaptive GIF speed
- GTK 4, Libadwaita, and PyGObject for the optional GUI
- A udev rule granting the desktop user read/write access to the cooler's
  `hidraw` device

Package names vary by distribution. On Arch Linux, the runtime dependencies
are available as:

```bash
sudo pacman -S python imagemagick ffmpeg lm_sensors nvtop gtk4 libadwaita python-gobject
```

OpenRGB is optional and is needed only for LED-derived GIF masking.

## Device permissions

Create a narrow udev rule for the observed USB device instead of making all
`hidraw` devices writable:

```bash
sudo tee /etc/udev/rules.d/70-darkflash-space-station.rules >/dev/null <<'EOF'
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="1d6b", ATTRS{idProduct}=="0102", TAG+="uaccess"
EOF
sudo udevadm control --reload-rules
sudo udevadm trigger
```

Disconnect and reconnect the cooler after installing the rule. Confirm that
the device is detected and accessible with:

```bash
darkflash-space-station status
```

If your hardware reports a different USB ID, do not broaden the rule. Open an
issue with the output of `lsusb` and the device name shown by
`udevadm info /dev/hidrawN` so support can be added explicitly.

## Installation

Clone the repository and create an editable virtual environment:

```bash
git clone https://github.com/brentmakesapps/dark-flash-space-station-linux.git
cd dark-flash-space-station-linux
python -m venv --system-site-packages .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
```

`--system-site-packages` is required because Arch provides PyGObject as a system
package. Activate the environment or prefix commands with `.venv/bin/`:

```bash
source .venv/bin/activate
darkflash-space-station status
```

## Command-line usage

```bash
# Query firmware, free media space, brightness, rotation, timeout, and mode.
darkflash-space-station status

# Resume the display and set its sleep timeout.
darkflash-space-station wake --timeout 60
darkflash-space-station display timeout 0

# Show a static foreground image.
darkflash-space-station media image ~/Downloads/hack-the-planet.jpg

# Upload a GIF once as a looping 20-FPS H.264 background.
darkflash-space-station media gif ~/Downloads/elmo-fire.gif

# Remove the foreground OSD to reveal the current video background.
darkflash-space-station media clear-overlay

# Send stock-theme state data, or render CPU/GPU values over the current video.
darkflash-space-station telemetry --once
darkflash-space-station telemetry --overlay --once
darkflash-space-station telemetry --overlay --layout Gaming
darkflash-space-station telemetry --overlay --temperature-unit F --once
darkflash-space-station telemetry --gpu-bdf 0000:04:00.0 --interval 1

# Serve the Home Assistant integration API, then select a saved layout.
darkflash-space-station api serve --port 8790
curl -X POST http://127.0.0.1:8790/api/layout -H 'Content-Type: application/json' -d '{"layout": "Gaming"}'
```

Run `darkflash-space-station --help` or append `--help` to any subcommand for
the complete argument reference. Saved layouts select a GPU and temperature
unit per widget. `--gpu-bdf` and `--temperature-unit` remain fallback values
for legacy layouts without those widget fields.

## GTK desktop app

Launch either command:

```bash
darkflash-space-station gui
darkflash-space-station-gui
```

To add **darkFlash Space Station** to the application launcher, make the GUI
entry point available on `PATH` and install the supplied desktop entry:

```bash
mkdir -p ~/.local/bin ~/.local/share/applications
ln -sf "$PWD/.venv/bin/darkflash-space-station-gui" \
  ~/.local/bin/darkflash-space-station-gui
install -m644 darkflash-space-station.desktop \
  ~/.local/share/applications/darkflash-space-station.desktop
```

The app provides:

- A sectioned Device, Telemetry, and Layout interface; the Layout section uses
  a layout thumbnail gallery, the selected layout's widgets with an Add button,
  a dominant live preview with bottom-right Save/Cancel actions, and a
  contextual control pane on the far right. The **Widgets** button beside
  **Media** restores or collapses the layout-widget list column, which starts
  hidden.
- **Device:** status refresh and wake.
- **Layout media:** the **Media** button beside the layout preview opens image
  and GIF controls in the same far-right pane used by widget controls. Each
  layout saves its own media file, GIF mask, hue, brightness, and adaptive-speed
  source. The selected image or animated GIF appears beneath the widgets in the
  live preview, and animated GIFs also play in each layout-selection thumbnail.
  Both previews use the layout's effective manual/OpenRGB mask, hue shift, and
  brightness so their colors match the rendered display background.
  Choosing media alone does not write to the device; **Apply** saves the active
  layout and restarts the Display service.
  Still images are encoded as looping H.264 backgrounds so telemetry remains on
  the separate foreground OSD layer. GIF masks can leave source colors
  unchanged, apply a manual luminance mask, or use the current first LED color
  from OpenRGB's `ARGB_V2_3` zone. OpenRGB masking requires its local SDK server
  (`openrgb --server`) to be running.
  Adaptive playback can pre-render ten cached variants: 0–10% load uses 1×,
  each additional 10% selects the next linear step, and 91–100% uses 3×.
  Speed can follow CPU load, a per-layout selected GPU's load, or the highest
  current load across the CPU and every detected GPU; a new range must remain
  active for five seconds before its cached MP4 is uploaded. OpenRGB
  color changes rebuild all ten variants before the updated background is sent.
  Hue rotation and brightness calibration can be applied to manual or OpenRGB
  mask colors; the running service detects tuning changes and rebuilds its
  cached variants automatically.
  **OpenRGB color sync** is automatic through the Display service whenever the
  OpenRGB mask is selected. It polls that LED every three seconds and rebuilds
  the cached variants when the adjusted color changes.
- **Display:** sleep timeout configuration.
- **Display runtime:** an always-on display service that prevents sleep,
  restores GIF media, manages adaptive playback, and renders the saved layout.
  A separate temporary preview can show the current unsaved layout.
  Each GPU widget has its own detected-GPU selector (for example, Intel Arc Pro
  B50), and each CPU/GPU temperature widget has its own Celsius/Fahrenheit
  selector. Telemetry also includes transparent overlays, one-shot update, and
  start/stop preview. Temperatures use a degree symbol (for example, `34°C` or
  `93°F`).
- **Telemetry layout:** named 320×320 layouts with an explicit **Save layout**
  button and a **Reset layout** option. Edits remain in memory until saved;
  Reset discards all pending layout edits and restores the selected saved
  layout. Selecting a saved layout immediately makes it active and restarts the
  Display service, while pending widget/media edits still require **Save** or
  **Apply** before the service uses them. Layouts support
  draggable CPU/GPU temperature and load, network upload/download, time, date,
  and custom-text widgets, plus per-widget font, size, and color controls.
  Text widgets can use either their saved manual color or follow OpenRGB's
  `ARGB_V2_3` color; the preview and Display service refresh that color every
  three seconds. OpenRGB text can apply its own −180° to +180° hue shift and
  25% to 200% brightness adjustment per widget.
  Selecting a widget reveals a lower-right resize handle: drag it to resize a
  text widget's font or a graph's width and height. Moving or resizing shows a
  translucent outline of the final position or size before the edit is applied.
  The font selector previews each installed family in its own typeface; choosing
  one updates the live layout preview immediately, while hovering or using
  Up/Down previews a candidate without changing the saved widget font.
  The preview includes 40-pixel grid lines to help align widgets. Enable
  **Snap to grid** to align moved widgets and resized graphs to those lines.
  Telemetry output is inset 24 pixels from the cooler panel's top and left
  edges to align the device's OSD coordinate origin with the preview.
  The **Add** menu also offers bar, line, circular-line, semicircle-gauge,
  ring-gauge, and pie graphs for every CPU/GPU temperature/load and
  network-throughput metric. Semicircle gauges show the latest value as a
  progress arc; ring gauges show the full ring using the selected color at
  50% opacity. The other graph types use a rolling history window (default:
  60 seconds). Circular-line, gauge, and pie graphs keep a 1:1 aspect ratio.
  Each graph's color, width, height, history duration, and position are saved
  in the layout JSON.

Errors appear as in-app notifications. The app serializes every HID write. Stop
the display service or temporary telemetry preview before a foreground media
operation so it is not waiting for the active writer to release the device.

Saved layouts are stored in
`~/.config/darkflash-space-station/layouts.json`. The selected GUI layout is
used for both one-shot and continuous GUI telemetry overlays. Saving a layout
also writes it as the active layout in
`~/.config/darkflash-space-station/active-layout`; `telemetry --overlay` uses
that saved active layout by default. Use `--layout NAME` to run a different
saved layout without changing the active one.
Graph entries use the same metric names as text widgets and add
`graph_style`, `width`, `height`, and `history_seconds` fields. Missing
`history_seconds` values default to 60 seconds, so existing layout files remain
compatible and can be edited or backed up as JSON.
Each layout object also contains a `media` object. Legacy layout files that
store only a widget list continue to inherit the previously retained global GIF
until their media is explicitly changed.

The GUI also saves its telemetry refresh interval, display sleep timeout, and
currently selected layout in
`~/.config/darkflash-space-station/settings.json`. These preferences are saved
when changed and are separate from editable layout data: widget/layout edits
still require **Save layout**.

## One writer at a time

The cooler supports only one active HID writer. Do not run the Wine DF Space
Station app, the installed telemetry service, and this CLI/GUI concurrently.
Close or stop the other writer before a direct media or display operation:

```bash
systemctl --user stop darkflash-space-station.service
```

The device can acknowledge invalid or incomplete media transfers while leaving
the panel unresponsive. If that occurs, disconnect and reconnect the cooler's
USB connection, then use `media image` or `wake` to restore it.

## Protocol notes

The display uses padded 1,024-byte HID reports. Control messages carry a
monotonic `SeqNumber`; media transfer uses `POST transport`, raw 1,000-byte
blocks, and `POST transported`. A transfer's raw-block acknowledgement has
`AckNumber=0` and is valid.

The display's accepted GIF path is not a host-streamed sequence of PNGs:

1. Convert GIF to a 320×320 H.264 High Profile, YUV420P, 20 FPS MP4.
2. Keep the video below the device's reported media-space limit (about 78 KB).
3. Upload it as `.mp4`.
4. Upload a transparent `.osd` to clear the foreground layer.

The current encoder targets 675 kbps; source GIF complexity and duration still
determine whether the converted media fits the device.

## Always-on display service

The user service prevents display sleep, restores and manages GIF media,
continuously renders the active saved telemetry layout as the foreground
overlay, and restarts after a failure. Keep it running even when the saved
layout has no telemetry widgets. It does not require root:

```bash
darkflash-space-station service install
darkflash-space-station service start
darkflash-space-station service status
darkflash-space-station service stop
```

`service install` safely writes the managed unit to
`~/.config/systemd/user/` and runs `systemctl --user daemon-reload`; `start`
also enables the service for future logins. The GTK app exposes the same
running/stopped status and Start/Stop control under **Display runtime**.

Stop the service before using foreground CLI or GUI media controls. It is one
of the possible HID writers described above.

The `service install` command writes a user unit containing the executable path
of the Python environment that ran the command. Re-run it after moving or
recreating the virtual environment.

## Home Assistant integration

The `api` command group serves a small HTTP API that Home Assistant automations
can use to select a saved layout and read live telemetry. No extra Python
packages are required. Applying a layout stores it as the active layout and
restarts the display service, the same path the GUI uses, so the panel updates
immediately.

| Method | Path | Returns |
| --- | --- | --- |
| `GET` | `/` | Service name and endpoint list |
| `GET` | `/api/state` | Active layout, saved layout names, and whether the display and API services are active |
| `GET` | `/api/telemetry` | CPU, GPU, and network values in the stock-theme payload shape |
| `POST` | `/api/layout` | `{"layout": "Night"}` — the name must already be saved; responds with the new state |

Requests use a `Bearer` token. Loopback access needs no token; binding to any
other address requires `--token`. Unknown layout names return `404`, a missing
or malformed body returns `400`, and an unreadable telemetry source returns
`502`.

Enable the API as a systemd user service, which writes the unit and a
token file at `~/.config/darkflash-space-station/api.env` (mode `0600`):

```bash
darkflash-space-station api install --bind 127.0.0.1 --port 8790 --token "$HA_TOKEN"
darkflash-space-station api start
darkflash-space-station api status
darkflash-space-station api stop
```

To run it in the foreground instead, use `api serve` with the same options.

### Home Assistant configuration

Point the API at Home Assistant's address if it runs on another host, for
example `--bind 0.0.0.0 --token "$HA_TOKEN"`. In Home Assistant:

```yaml
rest_command:
  darkflash_layout:
    url: http://127.0.0.1:8790/api/layout
    method: POST
    headers:
      authorization: Bearer REPLACE_WITH_TOKEN
      content-type: application/json
    payload: '{"layout": "{{ layout }}"}'

sensor:
  - platform: rest
    name: Space Station active layout
    unique_id: darkflash_active_layout
    resource: http://127.0.0.1:8790/api/state
    headers:
      authorization: Bearer REPLACE_WITH_TOKEN
    value_template: "{{ value_json.active_layout }}"

  - platform: rest
    name: Space Station CPU load
    unique_id: darkflash_cpu_load
    resource: http://127.0.0.1:8790/api/telemetry
    headers:
      authorization: Bearer REPLACE_WITH_TOKEN
    value_template: "{{ value_json.cpu.load }}"
    unit_of_measurement: "%"
    state_class: measurement

  - platform: rest
    name: Space Station GPU load
    unique_id: darkflash_gpu_load
    resource: http://127.0.0.1:8790/api/telemetry
    headers:
      authorization: Bearer REPLACE_WITH_TOKEN
    value_template: "{{ value_json.gpu.load }}"
    unit_of_measurement: "%"
    state_class: measurement

  - platform: rest
    name: Space Station GPU temperature
    unique_id: darkflash_gpu_temperature
    resource: http://127.0.0.1:8790/api/telemetry
    headers:
      authorization: Bearer REPLACE_WITH_TOKEN
    value_template: "{{ value_json.gpu.temperature }}"
    unit_of_measurement: "°C"
```

Each sensor polls its endpoint every 30 seconds by default; set `scan_interval` for faster updates. Replace `REPLACE_WITH_TOKEN` with the same value passed to `api install --token`.

Select a layout on a schedule:

```yaml
automation:
  - name: Space Station night layout
    trigger:
      - platform: time
        at: "21:00:00"
    action:
      - service: rest_command.darkflash_layout
        data:
          layout: Night

  - name: Space Station gaming layout
    trigger:
      - platform: numeric_state
        entity_id: sensor.space_station_gpu_load
        above: 80
        for: "00:05:00"
    action:
      - service: rest_command.darkflash_layout
        data:
          layout: Gaming
```

Layout names must match names saved in the GUI or `layouts.json`; the API only
selects existing layouts, it does not create them. Layout changes restart
the display service, so a layout switch cannot run at the same moment as a
manual media write — see the one-writer rule above.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| `display was not found on hidraw` | Reconnect the USB cable, verify `lsusb`, and make sure the device reports `1d6b:0102`. |
| `permission denied opening /dev/hidrawN` | Install the udev rule above, reload the rules, then reconnect the cooler. |
| `unable to render` or GIF conversion fails | Verify `convert`, `fc-list`, and an FFmpeg build with `libx264` are installed. |
| CPU temperature is unavailable | Run `sensors -j` and configure the required kernel sensor modules with `sensors-detect`. |
| OpenRGB masking fails | Start the local SDK server with `openrgb --server` and verify the `ARGB_V2_3` zone exists. |
| Panel stops responding after media upload | Stop every competing writer, reconnect the cooler, then run `wake` or upload a still image. |
| Background service repeatedly restarts | Run `darkflash-space-station service status` and inspect `journalctl --user -u darkflash-space-station.service`. |

## Development

Run the test suite from the repository root:

```bash
.venv/bin/pytest -q
```

The tests exercise protocol framing and CLI behavior without requiring the
physical display. Hardware, media-transfer, and telemetry changes should also
be verified on the target cooler.

## Project layout

| Path | Purpose |
| --- | --- |
| `src/darkflash_space_station/protocol.py` | HID message and media-transfer framing |
| `src/darkflash_space_station/hid.py` | Device discovery and raw HID I/O |
| `src/darkflash_space_station/controller.py` | High-level display operations |
| `src/darkflash_space_station/media.py` | Image, video, overlay, and graph rendering |
| `src/darkflash_space_station/telemetry.py` | CPU, GPU, and network telemetry |
| `src/darkflash_space_station/layout.py` | Saved telemetry layout model |
| `src/darkflash_space_station/gui.py` | GTK 4 / Libadwaita application |
| `src/darkflash_space_station/service.py` | Managed systemd user-service integration |
| `src/darkflash_space_station/api.py` | Home Assistant integration HTTP API |
| `tests/` | Protocol, CLI, and API tests |

## License

Licensed under the [GNU General Public License v3.0 only](LICENSE).
