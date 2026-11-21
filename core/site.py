"""The observing site: where the telescope is, and what blocks its view.

Everything that depends on place (is a transit above the horizon, is it dark, how far is the Moon, what does the
weather say) reads the site from :class:`config.Settings` (``site_*`` fields).  This module is the one place
those fields get their values from, in this order:

1. **Environment**: ``ASTRONOMO_SITE_LAT`` / ``_SITE_LON`` / ``_SITE_ELEV_M`` / ``_SITE_NAME`` (a deployment or a test).
2. **The site file**: ``<data directory>/site.json``, written by the dashboard's *Observing site* panel.  It is
   plain JSON a person can read and edit, and it stays on this computer.
3. **The placeholder** (Denver).  It exists so a fresh checkout runs, and nothing else: :attr:`Settings.site_source`
   is then ``"default"``, the dashboard says so at the top of the Opportunities page, and a mission on a real
   telescope refuses to start.

The site file may also hold a **horizon**: a few ``[azimuth, altitude]`` points (degrees, azimuth from north through
east) giving the lowest altitude the telescope can see at each bearing: a house to the south, trees to the west.
The line is drawn straight between points and wraps round at north.  Where it is higher than the site-wide minimum
altitude, it wins.

A place can be found by name with :func:`geocode` (Open-Meteo's free geocoding service, no key).  The result is
only ever a suggestion for a person to confirm and save; nothing is looked up unless the person asks.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

SITE_FILE = "site.json"
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
MAX_HORIZON_POINTS = 72


class SiteError(ValueError):
    """The site as given is not usable (out of range, malformed, or a horizon that makes no sense)."""


@dataclass(frozen=True)
class Place:
    """One result of a place search."""

    name: str
    lat: float
    lon: float
    elev_m: float | None
    detail: str  # "Colorado, United States"

    @property
    def label(self) -> str:
        return f"{self.name}, {self.detail}" if self.detail else self.name


def site_file(data_dir: Path) -> Path:
    return Path(data_dir) / SITE_FILE


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def validate_horizon(points: Any) -> tuple[tuple[float, float], ...]:
    """A horizon as sorted ``(azimuth, altitude)`` pairs, or an empty tuple.  Raises :class:`SiteError` if it makes no sense."""
    if not points:
        return ()
    if not isinstance(points, (list, tuple)) or len(points) > MAX_HORIZON_POINTS:
        raise SiteError(f"the horizon must be a list of at most {MAX_HORIZON_POINTS} [azimuth, altitude] points")
    out: dict[float, float] = {}
    for p in points:
        try:
            az, alt = float(p[0]), float(p[1])
        except (TypeError, ValueError, IndexError):
            raise SiteError(f"a horizon point must be [azimuth, altitude], not {p!r}") from None
        if not (math.isfinite(az) and math.isfinite(alt)) or not 0.0 <= az <= 360.0 or not 0.0 <= alt <= 89.0:
            raise SiteError(f"horizon point {p!r}: azimuth must be 0 to 360 and altitude 0 to 89 degrees")
        out[az % 360.0] = alt
    return tuple(sorted(out.items()))


def parse_horizon(text: str) -> tuple[tuple[float, float], ...]:
    """The text a person types, one ``azimuth altitude`` pair per line (commas or spaces), e.g. ``180 35``."""
    points = []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.replace(",", " ").replace(";", " ").split()
        if len(parts) != 2:
            raise SiteError(f"horizon line {n} ({line!r}) should be two numbers: azimuth then altitude")
        try:
            points.append([float(parts[0]), float(parts[1])])
        except ValueError:
            raise SiteError(f"horizon line {n} ({line!r}) should be two numbers: azimuth then altitude") from None
    return validate_horizon(points)


def horizon_text(horizon: tuple[tuple[float, float], ...]) -> str:
    return "\n".join(f"{az:g} {alt:g}" for az, alt in horizon)


def horizon_altitude(horizon: tuple[tuple[float, float], ...], azimuth_deg: Any) -> Any:
    """The lowest visible altitude at each azimuth (0 where there is no horizon).  Works on a number or an array."""
    az = np.asarray(azimuth_deg, dtype=float) % 360.0
    if not horizon:
        return np.zeros_like(az) if az.ndim else 0.0
    xs = np.array([p[0] for p in horizon], dtype=float)
    ys = np.array([p[1] for p in horizon], dtype=float)
    # wrap round at north so a point at 350 and one at 10 are joined across 0
    xs = np.concatenate([[xs[-1] - 360.0], xs, [xs[0] + 360.0]])
    ys = np.concatenate([[ys[-1]], ys, [ys[0]]])
    out = np.interp(az, xs, ys)
    return out if az.ndim else float(out)


def validated_site(name: str, lat: Any, lon: Any, elev_m: Any, horizon: Any = ()) -> dict[str, Any]:
    try:
        lat, lon, elev = float(lat), float(lon), float(elev_m)
    except (TypeError, ValueError):
        raise SiteError("latitude, longitude and elevation must be numbers") from None
    if not all(math.isfinite(x) for x in (lat, lon, elev)):
        raise SiteError("latitude, longitude and elevation must be finite numbers")
    if not -90.0 <= lat <= 90.0:
        raise SiteError(f"latitude {lat:g} is outside -90 to 90")
    if not -180.0 <= lon <= 180.0:
        raise SiteError(f"longitude {lon:g} is outside -180 to 180 (east is positive)")
    if not -500.0 <= elev <= 9000.0:
        raise SiteError(f"elevation {elev:g} m is outside -500 to 9000")
    name = str(name or "").strip() or "My site"
    return {"name": name[:80], "lat": round(lat, 5), "lon": round(lon, 5), "elev_m": round(elev, 1),
            "horizon": [list(p) for p in validate_horizon(horizon)]}


# --------------------------------------------------------------------------- #
# The site file
# --------------------------------------------------------------------------- #
def load_site_file(data_dir: Path) -> dict[str, Any] | None:
    """The saved site, validated, or ``None`` if there is none.  A damaged file is reported, not trusted."""
    path = site_file(data_dir)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
        return validated_site(raw.get("name"), raw.get("lat"), raw.get("lon"), raw.get("elev_m", 0.0), raw.get("horizon") or ())
    except (OSError, ValueError, AttributeError) as exc:
        log.error("Ignoring %s: %s", path, exc)
        return None


def save_site_file(data_dir: Path, name: str, lat: Any, lon: Any, elev_m: Any, horizon: Any = ()) -> dict[str, Any]:
    site = validated_site(name, lat, lon, elev_m, horizon)
    path = site_file(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(site, indent=2) + "\n")
    tmp.replace(path)
    return site


def _env_site_set() -> bool:
    return any(os.environ.get(f"ASTRONOMO_{k}") for k in ("SITE_LAT", "SITE_LON"))


def apply_site(settings):
    """``settings`` with its site fields resolved (environment, then the site file, then the placeholder).

    Called by :func:`config.get_settings`.  Settings built another way keep what they were given.
    """
    if _env_site_set():
        file_horizon = (load_site_file(settings.data_dir) or {}).get("horizon") or ()
        return replace(settings, site_source="environment", site_horizon=validate_horizon(file_horizon))
    saved = load_site_file(settings.data_dir)
    if saved is None:
        return settings  # the placeholder; site_source stays "default"
    return replace(
        settings, site_name=saved["name"], site_latitude_deg=saved["lat"], site_longitude_deg=saved["lon"],
        site_elevation_m=saved["elev_m"], site_source="file", site_horizon=validate_horizon(saved["horizon"]),
    )


def describe(settings) -> str:
    where = f"{settings.site_name} ({settings.site_latitude_deg:.3f}, {settings.site_longitude_deg:.3f}, {settings.site_elevation_m:.0f} m)"
    if settings.site_source == "default":
        return f"{where}: a placeholder, not your site"
    return where + (f", with a {len(settings.site_horizon)}-point horizon" if settings.site_horizon else "")


# --------------------------------------------------------------------------- #
# Finding a place by name
# --------------------------------------------------------------------------- #
def parse_geocoding(payload: Any) -> list[Place]:
    """Places from an Open-Meteo geocoding response (``{"results": [{name, latitude, longitude, elevation, ...}]}``)."""
    results = payload.get("results") if isinstance(payload, dict) else None
    out: list[Place] = []
    for r in results or []:
        try:
            lat, lon = float(r["latitude"]), float(r["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            continue
        elev = r.get("elevation")
        detail = ", ".join(str(x) for x in (r.get("admin1"), r.get("country")) if x)
        out.append(Place(str(r.get("name") or "Unnamed place"), lat, lon, float(elev) if isinstance(elev, (int, float)) else None, detail))
    return out


def geocode(query: str, *, count: int = 5, get_json=None) -> list[Place]:
    """Places matching ``query`` (a town, or a postal code where the service knows it).  Raises :class:`SiteError` if the lookup fails.

    ``get_json(url, params)`` is how the request is made (tests supply one); by default it is a plain HTTPS GET.
    The elevation the service gives is the ground at that point, a good first guess for a backyard but not a
    measurement: the page says so.
    """
    query = (query or "").strip()
    if len(query) < 2:
        raise SiteError("type at least two characters of a place name or postal code")
    params = {"name": query, "count": int(count), "language": "en", "format": "json"}
    try:
        if get_json is None:
            import requests

            resp = requests.get(GEOCODING_URL, params=params, timeout=10)
            resp.raise_for_status()
            payload = resp.json()
        else:
            payload = get_json(GEOCODING_URL, params)
    except Exception as exc:  # noqa: BLE001 - any network or parse failure is the same message to a person
        raise SiteError(f"the place search could not be reached ({exc}); enter the coordinates by hand") from exc
    return parse_geocoding(payload)
