"""Open-Meteo wrapper: cloud cover, wind, precipitation and a seeing *proxy*.

Design notes
------------
* Fail safe: if the service cannot be reached the caller gets :class:`WeatherUnavailable`
  and ``check_weather`` treats that as "not safe to observe" instead of guessing.
* Open-Meteo has no seeing product.  ``seeing_arcsec`` is a documented heuristic derived
  from the 300 hPa (jet stream) wind and surface wind.  Use it for ranking nights,
  never as a measurement.
* Offline mode returns deterministic synthetic conditions (clear unless
  ``settings.mock_cloudy_checks`` asks for cloudy initial checks, which demonstrates the
  SLEEP transition).
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import Settings

log = logging.getLogger(__name__)


class WeatherUnavailable(RuntimeError):
    pass


@dataclass
class WeatherReport:
    timestamp: str
    cloud_cover_pct: float
    wind_kmh: float
    precipitation_mm: float
    humidity_pct: float | None
    temperature_c: float | None
    seeing_arcsec: float | None
    seeing_note: str
    source: str  # "open-meteo" | "simulated"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def estimate_seeing_arcsec(jet_wind_kmh: float | None, surface_wind_kmh: float) -> float:
    """Heuristic seeing proxy (arcsec FWHM): worse with a fast jet stream and gusty ground."""
    jet = 0.0 if jet_wind_kmh is None else max(jet_wind_kmh - 40.0, 0.0)
    seeing = 1.0 + 0.012 * jet + 0.02 * surface_wind_kmh
    return round(min(max(seeing, 0.6), 6.0), 2)


# What a forecast verdict means for one observing window (cloud is the only thing that decides it).
CLEAR_MEAN_PCT, CLEAR_MAX_PCT = 25.0, 50.0
PARTLY_MEAN_PCT = 60.0


def _hour_start(row: dict[str, Any]) -> datetime:
    t = datetime.fromisoformat(str(row["time"]).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def summarise_window(hours: list[dict[str, Any]], start: datetime, end: datetime) -> dict[str, Any] | None:
    """Forecast for ``[start, end]`` from hourly rows (``time`` ISO UTC, ``cloud_cover_pct``, ...), or None.

    None means the forecast does not cover at least half of the window, so nothing is claimed.
    The verdict is ``clear`` (mean cloud under 25 % and never over 50 %, no rain), ``partly`` (mean under 60 %,
    no rain) or ``cloudy``.  Each forecast hour is the hour *starting* at its time.
    """
    if end < start:
        start, end = end, start
    span_h = max((end - start).total_seconds() / 3600.0, 1e-9)
    inside: list[dict[str, Any]] = []
    covered_h = 0.0
    for h in hours:
        if h.get("cloud_cover_pct") is None:
            continue
        t0 = _hour_start(h)
        overlap = (min(end, t0 + timedelta(hours=1)) - max(start, t0)).total_seconds() / 3600.0
        if overlap > 0:
            inside.append(h)
            covered_h += overlap
    if not inside or covered_h < 0.5 * span_h:
        return None
    clouds = [float(h["cloud_cover_pct"]) for h in inside]
    rain = sum(float(h.get("precipitation_mm") or 0.0) for h in inside)
    mean, peak = sum(clouds) / len(clouds), max(clouds)
    if rain <= 0.0 and mean < CLEAR_MEAN_PCT and peak <= CLEAR_MAX_PCT:
        verdict = "clear"
    elif rain <= 0.0 and mean < PARTLY_MEAN_PCT:
        verdict = "partly"
    else:
        verdict = "cloudy"
    return {"verdict": verdict, "cloud_mean_pct": mean, "cloud_max_pct": peak, "precip_mm": rain, "hours": len(inside)}


class WeatherClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.offline = settings.offline_services
        self._mock_calls = 0
        self._session = requests.Session()
        retry = Retry(total=3, backoff_factor=1.0, status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("GET",))
        self._session.mount("https://", HTTPAdapter(max_retries=retry))

    # ------------------------------------------------------------------ #
    def get_conditions(self, lat: float | None = None, lon: float | None = None) -> WeatherReport:
        lat = self.settings.site_latitude_deg if lat is None else lat
        lon = self.settings.site_longitude_deg if lon is None else lon
        if self.offline:
            return self._simulated()
        try:
            return self._fetch(lat, lon)
        except WeatherUnavailable:
            saved = conditions_from_saved(self.settings, lat, lon)
            if saved is None:
                raise
            log.warning("Live forecast failed; using the forecast saved at %s", saved.timestamp)
            return saved

    def get_forecast(self, lat: float | None = None, lon: float | None = None, days: int = 7) -> list[dict[str, Any]]:
        """Hourly forecast for the next ``days`` days: ``[{"time", "cloud_cover_pct", "precipitation_mm", "wind_kmh"}]``.

        Times are UTC ISO strings, each the start of its hour.  Raises :class:`WeatherUnavailable`.  Offline, a
        deterministic synthetic forecast (flagged by ``source``) stands in so nothing touches the network.
        """
        lat = self.settings.site_latitude_deg if lat is None else lat
        lon = self.settings.site_longitude_deg if lon is None else lon
        days = max(1, min(int(days), 16))
        if self.offline:
            return self._simulated_forecast(days)
        params: dict[str, Any] = {
            "latitude": lat, "longitude": lon,
            "hourly": "cloud_cover,precipitation,wind_speed_10m,relative_humidity_2m,temperature_2m",
            "forecast_days": days, "timezone": "UTC", "wind_speed_unit": "kmh",
        }
        url = self.settings.open_meteo_url
        if self.settings.open_meteo_api_key:
            params["apikey"] = self.settings.open_meteo_api_key
            url = self.settings.open_meteo_commercial_url
        try:
            resp = self._session.get(url, params=params, timeout=20)
            resp.raise_for_status()
            hourly = (resp.json() or {}).get("hourly") or {}
        except (requests.RequestException, ValueError) as exc:
            raise WeatherUnavailable(f"Open-Meteo forecast request failed: {exc}") from exc
        times, cloud = hourly.get("time") or [], hourly.get("cloud_cover") or []
        if not times or len(times) != len(cloud):
            raise WeatherUnavailable("Open-Meteo forecast had no hourly cloud cover")
        rain, wind = hourly.get("precipitation") or [], hourly.get("wind_speed_10m") or []
        humid, temp = hourly.get("relative_humidity_2m") or [], hourly.get("temperature_2m") or []
        out = []
        for i, (t, c) in enumerate(zip(times, cloud)):
            if c is None:
                continue
            out.append({
                "time": str(t) + ("" if str(t).endswith("Z") or "+" in str(t) else "+00:00"),
                "cloud_cover_pct": float(c),
                "precipitation_mm": float(rain[i] or 0.0) if i < len(rain) else 0.0,
                "wind_kmh": float(wind[i] or 0.0) if i < len(wind) else 0.0,
                "humidity_pct": None if i >= len(humid) or humid[i] is None else float(humid[i]),
                "temperature_c": None if i >= len(temp) or temp[i] is None else float(temp[i]),
                "source": "open-meteo",
            })
        return out

    @staticmethod
    def _simulated_forecast(days: int) -> list[dict[str, Any]]:
        start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        out = []
        for h in range(days * 24):
            t = start + timedelta(hours=h)
            cloud = 50.0 + 45.0 * math.sin(2 * math.pi * (h / 24.0 / 2.7))  # a cloudy spell every few days
            out.append({"time": t.isoformat(), "cloud_cover_pct": round(min(max(cloud, 0.0), 100.0), 1),
                        "precipitation_mm": 0.0, "wind_kmh": 8.0, "humidity_pct": 45.0, "temperature_c": 8.0,
                        "source": "simulated"})
        return out

    # ------------------------------------------------------------------ #
    def _simulated(self) -> WeatherReport:
        self._mock_calls += 1
        cloudy = self._mock_calls <= self.settings.mock_cloudy_checks
        cloud = 78.0 - 6.0 * (self._mock_calls - 1) if cloudy else 8.0 + (self._mock_calls % 3) * 2.0
        return WeatherReport(
            timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            cloud_cover_pct=float(cloud),
            wind_kmh=6.0,
            precipitation_mm=0.0,
            humidity_pct=45.0,
            temperature_c=8.0,
            seeing_arcsec=1.8,
            seeing_note="simulated",
            source="simulated",
        )

    def _fetch(self, lat: float, lon: float) -> WeatherReport:
        params: dict[str, Any] = {
            "latitude": lat,
            "longitude": lon,
            "current": "cloud_cover,wind_speed_10m,precipitation,relative_humidity_2m,temperature_2m",
            "hourly": "wind_speed_300hPa",
            "forecast_days": 1,
            "timezone": "UTC",
            "wind_speed_unit": "kmh",
        }
        url = self.settings.open_meteo_url
        if self.settings.open_meteo_api_key:
            params["apikey"] = self.settings.open_meteo_api_key
            url = self.settings.open_meteo_commercial_url
        try:
            resp = self._session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise WeatherUnavailable(f"Open-Meteo request failed: {exc}") from exc

        cur = data.get("current") or {}
        if "cloud_cover" not in cur or cur["cloud_cover"] is None:
            raise WeatherUnavailable(f"Open-Meteo response had no cloud_cover: {data}")

        wind = float(cur.get("wind_speed_10m") or 0.0)
        jet = self._jet_wind(data, cur.get("time"))
        return WeatherReport(
            timestamp=str(cur.get("time") or datetime.now(timezone.utc).isoformat(timespec="seconds")),
            cloud_cover_pct=float(cur["cloud_cover"]),
            wind_kmh=wind,
            precipitation_mm=float(cur.get("precipitation") or 0.0),
            humidity_pct=cur.get("relative_humidity_2m"),
            temperature_c=cur.get("temperature_2m"),
            seeing_arcsec=estimate_seeing_arcsec(jet, wind),
            seeing_note="heuristic proxy from 300 hPa + surface wind (not a measurement)",
            source="open-meteo",
        )

    @staticmethod
    def _jet_wind(data: dict, current_time: str | None) -> float | None:
        hourly = data.get("hourly") or {}
        times, winds = hourly.get("time") or [], hourly.get("wind_speed_300hPa") or []
        if not times or not winds or not current_time:
            return None
        hour = str(current_time)[:13]  # "YYYY-MM-DDTHH"
        for t, w in zip(times, winds):
            if str(t)[:13] == hour:
                return None if w is None else float(w)
        return None


def forecast_path(settings: Settings) -> Path:
    """Where ``--fetch-forecast`` keeps the hourly forecast for a later run with no internet."""
    return settings.data_dir / "weather_forecast.json"


def save_forecast(settings: Settings, hours: list[dict[str, Any]], *, lat: float | None = None, lon: float | None = None) -> Path:
    """Write ``hours`` (the rows :meth:`WeatherClient.get_forecast` returns) for this site."""
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "latitude_deg": settings.site_latitude_deg if lat is None else lat,
        "longitude_deg": settings.site_longitude_deg if lon is None else lon,
        "hours": hours,
    }
    path = forecast_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def load_forecast(settings: Settings) -> dict[str, Any] | None:
    path = forecast_path(settings)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def saved_hours(settings: Settings) -> list[dict[str, Any]] | None:
    """Hourly rows from the saved forecast, or None when there is no file."""
    data = load_forecast(settings)
    hours = (data or {}).get("hours")
    return list(hours) if isinstance(hours, list) else None


def _close(a: Any, b: float, tol: float = 0.2) -> bool:
    try:
        return abs(float(a) - b) <= tol
    except (TypeError, ValueError):
        return False


def _parse_stamp(value: str) -> datetime:
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def conditions_from_saved(
    settings: Settings, lat: float, lon: float, now: datetime | None = None
) -> WeatherReport | None:
    """The saved hour that covers ``now``, or None if the file is missing, stale, for another site, or has a gap.

    A saved hour is a measurement. Callers must not treat it as ``ASTRONOMO_ASSUME_CLEAR``.
    """
    data = load_forecast(settings)
    if not data or not _close(data.get("latitude_deg"), lat) or not _close(data.get("longitude_deg"), lon):
        return None
    try:
        fetched = _parse_stamp(data["fetched_at"])
    except (KeyError, TypeError, ValueError):
        return None
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if (now - fetched).total_seconds() > float(settings.forecast_max_age_h) * 3600.0:
        return None
    chosen: dict[str, Any] | None = None
    for hour in data.get("hours") or []:
        if not isinstance(hour, dict) or hour.get("cloud_cover_pct") is None:
            continue
        try:
            start = _parse_stamp(hour["time"])
        except (KeyError, TypeError, ValueError):
            continue
        if start <= now < start + timedelta(hours=1):
            chosen = hour
            break
    if chosen is None:
        return None
    wind = float(chosen.get("wind_kmh") or 0.0)
    humid, temp = chosen.get("humidity_pct"), chosen.get("temperature_c")
    return WeatherReport(
        timestamp=str(chosen["time"]),
        cloud_cover_pct=float(chosen["cloud_cover_pct"]),
        wind_kmh=wind,
        precipitation_mm=float(chosen.get("precipitation_mm") or 0.0),
        humidity_pct=None if humid is None else float(humid),
        temperature_c=None if temp is None else float(temp),
        seeing_arcsec=estimate_seeing_arcsec(None, wind),
        seeing_note="saved forecast (fetched before this computer left the internet); not a live reading",
        source="saved-forecast",
    )
