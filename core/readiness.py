"""Dew, battery and free-space checks for a night nobody is watching.

Dew uses the forecast's temperature and humidity (Magnus formula). There is no dew sensor. Battery is whatever
the telescope reports; ``None`` means it did not, and an unattended start will not guess. Free space is the
disk the frames are written to, which this computer can measure.
"""

from __future__ import annotations

import math
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Reading:
    name: str
    ok: bool
    known: bool
    detail: str


def dew_point_c(temperature_c: float, humidity_pct: float) -> float:
    """Dew point (°C) from air temperature and relative humidity. Magnus formula over water."""
    a, b = 17.625, 243.04
    rh = min(max(float(humidity_pct), 0.1), 100.0)
    gamma = math.log(rh / 100.0) + (a * float(temperature_c)) / (b + float(temperature_c))
    return (b * gamma) / (a - gamma)


def dew_reading(temperature_c: float | None, humidity_pct: float | None, margin_c: float) -> Reading:
    if temperature_c is None or humidity_pct is None:
        return Reading("dew", False, False, "the forecast has no temperature or humidity, so the dew margin is unknown")
    margin = float(temperature_c) - dew_point_c(float(temperature_c), float(humidity_pct))
    if margin < margin_c:
        return Reading("dew", False, True, f"dew margin {margin:.1f} C is under {margin_c:.0f} C")
    return Reading("dew", True, True, f"dew margin {margin:.1f} C")


def battery_reading(percent: float | None, minimum: float) -> Reading:
    if percent is None:
        return Reading("battery", False, False, "the telescope did not report a battery level")
    if float(percent) < minimum:
        return Reading("battery", False, True, f"battery {float(percent):.0f}% is under {minimum:.0f}%")
    return Reading("battery", True, True, f"battery {float(percent):.0f}%")


def disk_reading(path: Path, min_bytes: int) -> Reading:
    try:
        free = shutil.disk_usage(path).free
    except OSError as exc:
        return Reading("storage", False, False, f"could not read free space on {path}: {exc}")
    gb = free / 1e9
    need = min_bytes / 1e9
    if free < min_bytes:
        return Reading("storage", False, True, f"free space {gb:.1f} GB is under the {need:.1f} GB minimum")
    return Reading("storage", True, True, f"free space {gb:.1f} GB")


def start_blockers(readings: list[Reading], *, unattended: bool) -> list[str]:
    """Why a start should be refused.

    A person who confirmed the start is told when the battery or the dew margin is simply unknown, and the start
    is allowed: they are there. An unattended start is not. A reading that is known and bad blocks either start.
    """
    blockers: list[str] = []
    for reading in readings:
        if reading.ok:
            continue
        if not reading.known and not unattended and reading.name in {"battery", "dew"}:
            continue
        if not reading.known:
            blockers.append(f"{reading.name} is unknown; an unattended start will not guess ({reading.detail})")
        else:
            blockers.append(reading.detail)
    return blockers
