"""Native Linux telemetry needed by the stock Space Station status theme."""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable


class TelemetryError(RuntimeError):
    """A required local telemetry source is unavailable."""


GRAPH_METRICS = {
    "cpu-temperature": ("cpu", "temperature"),
    "cpu-load": ("cpu", "load"),
    "gpu-temperature": ("gpu", "temperature"),
    "gpu-load": ("gpu", "load"),
    "network-upload": ("network", "upload"),
    "network-download": ("network", "download"),
}


class TelemetryHistory:
    """Keep numeric graph readings for a bounded rolling time window."""

    def __init__(self, seconds: float = 60, *, clock: Callable[[], float] = time.monotonic) -> None:
        if seconds <= 0:
            raise ValueError("history duration must be greater than zero")
        self.seconds = seconds
        self._clock = clock
        self._samples: dict[str, deque[tuple[float, float]]] = {
            metric: deque() for metric in GRAPH_METRICS
        }

    def add(self, payload: Mapping[str, object]) -> None:
        now = self._clock()
        for metric, (section, field) in GRAPH_METRICS.items():
            value = payload.get(section)
            if isinstance(value, Mapping) and isinstance(value.get(field), (int, float)):
                self._samples[metric].append((now, float(value[field])))
        gpus = payload.get("gpus")
        if isinstance(gpus, Mapping):
            for bdf, gpu in gpus.items():
                if not isinstance(bdf, str) or not isinstance(gpu, Mapping):
                    continue
                for metric, field in (
                    ("gpu-temperature", "temperature"),
                    ("gpu-load", "load"),
                ):
                    value = gpu.get(field)
                    if isinstance(value, (int, float)):
                        self._samples.setdefault(f"{metric}@{bdf}", deque()).append(
                            (now, float(value))
                        )
        self._prune(now)

    def values(self, metric: str, seconds: float | None = None) -> list[float]:
        """Return readings from the requested trailing window."""
        now = self._clock()
        self._prune(now)
        if seconds is None:
            seconds = self.seconds
        if seconds <= 0:
            raise ValueError("history duration must be greater than zero")
        cutoff = now - min(seconds, self.seconds)
        return [
            value
            for timestamp, value in self._samples.get(metric, ())
            if timestamp >= cutoff
        ]

    def _prune(self, now: float) -> None:
        cutoff = now - self.seconds
        for samples in self._samples.values():
            while samples and samples[0][0] < cutoff:
                samples.popleft()


class TemperatureAverage:
    """Average temperature samples from a short trailing time window."""

    def __init__(self, seconds: float = 5, *, clock: Callable[[], float] = time.monotonic) -> None:
        if seconds <= 0:
            raise ValueError("temperature averaging duration must be greater than zero")
        self.seconds = seconds
        self._clock = clock
        self._samples: deque[tuple[float, float]] = deque()

    def add(self, temperature: float) -> float:
        now = self._clock()
        self._samples.append((now, temperature))
        cutoff = now - self.seconds
        while self._samples and self._samples[0][0] <= cutoff:
            self._samples.popleft()
        return sum(value for _, value in self._samples) / len(self._samples)


@dataclass(frozen=True)
class DetectedGpu:
    """A PCI display controller usable as a telemetry target."""

    bdf: str
    driver: str
    name: str

    @property
    def label(self) -> str:
        return self.name


@lru_cache
def _pci_name(vendor_id: str, device_id: str, pci_ids: Path) -> str | None:
    """Look up a PCI vendor/device pair in the standard pci.ids database."""
    try:
        lines = pci_ids.read_text(errors="replace").splitlines()
    except OSError:
        return None
    vendor_name: str | None = None
    for index, line in enumerate(lines):
        if line.startswith(f"{vendor_id} "):
            vendor_name = line.split(maxsplit=1)[1]
            for candidate in lines[index + 1 :]:
                if re.match(r"^[0-9a-f]{4}\s", candidate):
                    break
                if candidate.startswith(f"\t{device_id} "):
                    device_name = candidate.split(maxsplit=1)[1]
                    match = re.search(r"\[([^\]]+)\]", device_name)
                    if match and match.group(1).casefold() not in {
                        "intel graphics",
                        "vga compatible controller",
                    }:
                        device_name = match.group(1)
                    elif match:
                        device_name = device_name[: match.start()].strip()
                    vendor_name = re.sub(
                        r"\s+(?:Corporation|Inc\.?|Ltd\.?)$", "", vendor_name
                    )
                    if device_name.casefold().startswith(vendor_name.casefold()):
                        return device_name
                    return f"{vendor_name} {device_name}"
            break
    return None


def detected_gpus(
    pci_root: Path = Path("/sys/bus/pci/devices"),
    pci_ids: Path = Path("/usr/share/hwdata/pci.ids"),
) -> list[DetectedGpu]:
    """Return detected PCI display controllers and their bound drivers."""
    gpus: list[DetectedGpu] = []
    for device in sorted(pci_root.iterdir()):
        try:
            device_class = (device / "class").read_text().strip()
        except OSError:
            continue
        if not device_class.startswith("0x03"):
            continue
        driver_path = device / "driver"
        driver = driver_path.resolve().name if driver_path.exists() else "unbound"
        try:
            vendor_id = (device / "vendor").read_text().strip().removeprefix("0x")
            device_id = (device / "device").read_text().strip().removeprefix("0x")
        except OSError:
            vendor_id = device_id = ""
        name = _pci_name(vendor_id, device_id, pci_ids) or device.name
        gpus.append(DetectedGpu(device.name, driver, name))
    return gpus


def _sensors() -> dict:
    try:
        output = subprocess.run(
            ["sensors", "-j"], check=True, capture_output=True, text=True
        ).stdout
        data = json.loads(output)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise TelemetryError(f"unable to read sensors: {exc}") from exc
    return data if isinstance(data, dict) else {}


def _temperature(entries: object, label: str) -> float | None:
    if not isinstance(entries, dict) or not isinstance(entries.get(label), dict):
        return None
    return next(
        (
            float(value)
            for key, value in entries[label].items()
            if key.endswith("_input") and isinstance(value, (int, float))
        ),
        None,
    )


def cpu_temperature_c() -> float:
    """Read the Intel CPU package temperature from lm-sensors."""
    for chip, entries in _sensors().items():
        if chip.startswith("coretemp"):
            temperature = _temperature(entries, "Package id 0")
            if temperature is not None:
                return temperature
    raise TelemetryError("CPU package temperature is unavailable")


def _gpu_bdf(hwmon: Path) -> str | None:
    for part in reversed((hwmon / "device").resolve().parts):
        match = re.fullmatch(r"(?:[0-9a-f]{4}:)?([0-9a-f]{2}:[0-9a-f]{2}\.[0-7])", part)
        if match is not None:
            return f"0000:{match.group(1)}"
    return None


def gpu_temperature_c(
    gpu_bdf: str, hwmon_root: Path = Path("/sys/class/hwmon")
) -> float | None:
    """Read the selected GPU package temperature, when the GPU is present."""
    for hwmon in sorted(hwmon_root.glob("hwmon*")):
        try:
            driver = (hwmon / "name").read_text().strip()
        except OSError:
            continue
        if driver not in {"xe", "i915", "amdgpu", "nvidia", "nouveau"}:
            continue
        if _gpu_bdf(hwmon) != gpu_bdf:
            continue
        for input_path in sorted(hwmon.glob("temp*_input")):
            label_path = input_path.with_name(
                input_path.name.removesuffix("_input") + "_label"
            )
            try:
                label = label_path.read_text().strip().lower()
                temperature = int(input_path.read_text().strip()) / 1000
            except (OSError, ValueError):
                continue
            if label in {"pkg", "edge", "gpu", "junction"}:
                return temperature
    return None


def _nvtop_snapshot() -> list[dict]:
    try:
        snapshot = json.loads(
            subprocess.run(
                ["nvtop", "--snapshot", "--no-color", "--no-plot", "--no-processes"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        )
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise TelemetryError(f"unable to read nvtop GPU utilization: {exc}") from exc
    if not isinstance(snapshot, list):
        raise TelemetryError("nvtop did not return a device list")
    return [device for device in snapshot if isinstance(device, dict)]


def _nvtop_device_for_gpu(gpu: DetectedGpu, snapshot: list[dict]) -> dict | None:
    target_words = set(re.findall(r"[a-z0-9]+", gpu.name.lower())) - {
        "corporation",
        "controller",
        "graphics",
        "intel",
        "nvidia",
        "radeon",
    }
    if not target_words:
        return None
    scored_matches = [
        (
            len(
                target_words
                & set(
                    re.findall(
                        r"[a-z0-9]+", str(device.get("device_name")).lower()
                    )
                )
            ),
            device,
        )
        for device in snapshot
    ]
    best_score = max((score for score, _device in scored_matches), default=0)
    matches = [device for score, device in scored_matches if score == best_score]
    return matches[0] if best_score >= 2 and len(matches) == 1 else None


def gpu_utilization_percent(
    gpu_bdf: str, snapshot: list[dict] | None = None
) -> float:
    """Read selected-GPU utilization using the same nvtop source as PC Monitor."""
    gpu = next((device for device in detected_gpus() if device.bdf == gpu_bdf), None)
    if gpu is None:
        raise TelemetryError(f"GPU {gpu_bdf!r} was not found")
    device = _nvtop_device_for_gpu(gpu, snapshot or _nvtop_snapshot())
    if device is None:
        raise TelemetryError(f"nvtop did not identify GPU {gpu.name!r}")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)%", str(device.get("gpu_util")))
    if match is None:
        raise TelemetryError(f"nvtop did not report utilization for {gpu.name!r}")
    return max(0, min(100, float(match.group(1))))


def _cpu_ticks(path: Path = Path("/proc/stat")) -> tuple[int, int]:
    try:
        fields = path.read_text().splitlines()[0].split()
        values = [int(value) for value in fields[1:]]
    except (OSError, IndexError, ValueError) as exc:
        raise TelemetryError(f"unable to read CPU utilization: {exc}") from exc
    return sum(values), values[3] + values[4]


class CpuUtilization:
    """Calculates CPU busy percentage from successive `/proc/stat` snapshots."""

    def __init__(self) -> None:
        self._previous = _cpu_ticks()

    def read(self) -> float:
        current = _cpu_ticks()
        total_delta = current[0] - self._previous[0]
        idle_delta = current[1] - self._previous[1]
        self._previous = current
        return 0 if total_delta <= 0 else 100 * (total_delta - idle_delta) / total_delta


class NetworkThroughput:
    """Calculates aggregate non-loopback network throughput in KB/s."""

    def __init__(self) -> None:
        self._previous = self._bytes()
        self._time = time.monotonic()

    @staticmethod
    def _bytes() -> tuple[int, int]:
        received = sent = 0
        for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
            name, values = line.split(":", 1)
            if name.strip() == "lo":
                continue
            fields = values.split()
            received += int(fields[0])
            sent += int(fields[8])
        return received, sent

    def read(self) -> tuple[float, float]:
        current, now = self._bytes(), time.monotonic()
        elapsed = max(now - self._time, 0.001)
        download = (current[0] - self._previous[0]) / elapsed / 1024
        upload = (current[1] - self._previous[1]) / elapsed / 1024
        self._previous, self._time = current, now
        return upload, download


def state(
    cpu_usage: CpuUtilization,
    gpu_bdf: str,
    network: NetworkThroughput | None = None,
    temperature_average: TemperatureAverage | None = None,
    *,
    include_gpu_load: bool = False,
    gpu_bdfs: Iterable[str] | None = None,
) -> dict[str, object]:
    """Return the stock theme's complete CPU/GPU status payload."""
    usage = round(cpu_usage.read())
    upload, download = network.read() if network is not None else (0, 0)
    cpu_temperature = cpu_temperature_c()
    if temperature_average is not None:
        cpu_temperature = temperature_average.add(cpu_temperature)
    requested_gpus = list(dict.fromkeys([gpu_bdf, *(gpu_bdfs or ())]))
    utilization_snapshot = _nvtop_snapshot() if include_gpu_load else None
    gpus = {}
    for requested_bdf in requested_gpus:
        gpu_temperature = gpu_temperature_c(requested_bdf)
        gpus[requested_bdf] = {
            "load": (
                round(
                    gpu_utilization_percent(
                        requested_bdf, utilization_snapshot
                    )
                )
                if include_gpu_load
                else 0
            ),
            "temperature": (
                round(gpu_temperature) if gpu_temperature is not None else 0
            ),
            "fan": 0,
            "speed": 0,
            "power": 0,
            "voltage": 0,
            "memoryUsage": 0,
            "dedicated": 0,
            "dedicatedTotal": 0,
        }
    primary_gpu = gpus[gpu_bdf]
    return {
        "network": {"upload": round(upload, 1), "download": round(download, 1)},
        "memory": {"total": 0, "used": 0, "load": 0, "temperature": 0, "speed": 0},
        "cpu": {
            "load": usage,
            "temperature": round(cpu_temperature),
            "speedAverage": 0,
            "power": 0,
            "voltage": 0,
            "usage": usage,
        },
        "gpu": primary_gpu,
        "gpus": gpus,
        "disk": {
            "total": 0,
            "used": 0,
            "load": 0,
            "activity": 0,
            "temperature": 0,
            "readSpeed": 0,
            "writeSpeed": 0,
        },
        "fans": [],
        "motherboard": {"temperature": 0},
        "timestamp": int(time.time() * 1000),
    }
