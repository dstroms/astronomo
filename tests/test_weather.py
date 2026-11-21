import pytest
import requests

from config import get_settings
from core.graph import _evaluate_weather
from tools.weather import WeatherClient, WeatherUnavailable, estimate_seeing_arcsec


def test_offline_weather_is_clear_by_default(settings):
    r = WeatherClient(settings).get_conditions()
    assert r.source == "simulated" and r.cloud_cover_pct <= settings.cloud_cover_threshold_pct


def test_offline_cloudy_checks_then_clear():
    s = get_settings(offline_services=True, mock_cloudy_checks=2)
    wc = WeatherClient(s)
    clouds = [wc.get_conditions().cloud_cover_pct for _ in range(3)]
    assert clouds[0] > 30 and clouds[1] > 30 and clouds[2] <= 30


@pytest.mark.parametrize(
    "report,clear",
    [
        ({"cloud_cover_pct": 30.0, "wind_kmh": 5, "precipitation_mm": 0}, True),   # threshold is strictly '>'
        ({"cloud_cover_pct": 30.1, "wind_kmh": 5, "precipitation_mm": 0}, False),
        ({"cloud_cover_pct": 5.0, "wind_kmh": 55, "precipitation_mm": 0}, False),
        ({"cloud_cover_pct": 5.0, "wind_kmh": 5, "precipitation_mm": 0.2}, False),
        ({"cloud_cover_pct": None}, False),  # fail-safe on missing data
    ],
)
def test_weather_gate(report, clear):
    ok, _reason = _evaluate_weather(report, get_settings())
    assert ok is clear


def test_sun_altitude_matches_known_geometry():
    from datetime import datetime, timezone

    from tools.astrometry import sun_altitude_deg

    noon = sun_altitude_deg(39.7392, -104.9903, 1609, datetime(2026, 6, 21, 18, 58, tzinfo=timezone.utc))
    assert noon == pytest.approx(90 - 39.7392 + 23.44, abs=1.0)
    assert sun_altitude_deg(39.7392, -104.9903, 1609, datetime(2026, 6, 21, 7, 0, tzinfo=timezone.utc)) < -12


def test_assume_clear_replaces_a_missing_forecast_and_still_blocks_daylight():
    night = {"cloud_cover_pct": None, "sun_altitude_deg": -20.0}
    ok, reason = _evaluate_weather(night, get_settings(assume_clear_sky=True, mock_mode=False))
    assert ok and "operator asserted a clear sky" in reason
    day = {"cloud_cover_pct": None, "sun_altitude_deg": 25.0}
    ok, reason = _evaluate_weather(day, get_settings(assume_clear_sky=True, mock_mode=False))
    assert not ok and "not dark enough" in reason
    # a forecast that did arrive is still enforced; the switch only covers a missing one
    cloudy = {"cloud_cover_pct": 90.0, "wind_kmh": 0, "precipitation_mm": 0, "sun_altitude_deg": -20.0}
    assert _evaluate_weather(cloudy, get_settings(assume_clear_sky=True, mock_mode=False))[0] is False


def test_darkness_interlock_applies_to_real_hardware_only():
    clear_sky = {"cloud_cover_pct": 0.0, "wind_kmh": 0, "precipitation_mm": 0, "sun_altitude_deg": 25.0}
    ok, reason = _evaluate_weather(clear_sky, get_settings(mock_mode=False))
    assert not ok and "not dark enough" in reason
    assert _evaluate_weather({**clear_sky, "sun_altitude_deg": -20.0}, get_settings(mock_mode=False))[0]
    # the graph only *computes* sun altitude outside mock mode, so mock reports never carry it
    assert _evaluate_weather({k: v for k, v in clear_sky.items() if k != "sun_altitude_deg"}, get_settings(mock_mode=True))[0]


class _Resp:
    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError(f"{self.status}")

    def json(self):
        return self.payload


def _live_client(monkeypatch, resp):
    wc = WeatherClient(get_settings(offline_services=False))
    monkeypatch.setattr(wc._session, "get", lambda *a, **k: resp)
    return wc


def test_open_meteo_parsing_picks_matching_hour_for_jet_wind(monkeypatch):
    payload = {
        "current": {"time": "2026-10-20T22:15", "cloud_cover": 12, "wind_speed_10m": 10.0,
                    "precipitation": 0.0, "relative_humidity_2m": 40, "temperature_2m": 9.0},
        "hourly": {"time": ["2026-10-20T21:00", "2026-10-20T22:00"], "wind_speed_300hPa": [50.0, 140.0]},
    }
    r = _live_client(monkeypatch, _Resp(payload)).get_conditions()
    assert r.cloud_cover_pct == 12 and r.source == "open-meteo"
    assert r.seeing_arcsec == estimate_seeing_arcsec(140.0, 10.0)


def test_network_failure_raises_instead_of_assuming_clear(monkeypatch):
    wc = WeatherClient(get_settings(offline_services=False))

    def boom(*a, **k):
        raise requests.ConnectionError("no route")

    monkeypatch.setattr(wc._session, "get", boom)
    with pytest.raises(WeatherUnavailable):
        wc.get_conditions()


def test_missing_cloud_cover_raises(monkeypatch):
    with pytest.raises(WeatherUnavailable):
        _live_client(monkeypatch, _Resp({"current": {"time": "x"}})).get_conditions()


def test_seeing_proxy_is_monotonic_in_wind():
    assert estimate_seeing_arcsec(30, 5) < estimate_seeing_arcsec(150, 5) < estimate_seeing_arcsec(250, 30)


# --------------------------------------------------------------------------- #
# Forecast
# --------------------------------------------------------------------------- #
def test_offline_forecast_is_a_deterministic_week_flagged_simulated(settings):
    from dataclasses import replace

    rows = WeatherClient(replace(settings, offline_services=True)).get_forecast(days=7)
    assert len(rows) == 7 * 24 and {r["source"] for r in rows} == {"simulated"}
    assert all(0.0 <= r["cloud_cover_pct"] <= 100.0 for r in rows)


def test_live_forecast_is_parsed_and_asks_for_the_site_and_the_hourly_fields(settings, monkeypatch):
    from dataclasses import replace

    seen = {}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"hourly": {"time": ["2026-10-28T00:00", "2026-10-28T01:00", "2026-10-28T02:00"],
                               "cloud_cover": [10, None, 30], "precipitation": [0.0, 0.0, 0.2], "wind_speed_10m": [4.0, 5.0, 6.0]}}

    client = WeatherClient(replace(settings, offline_services=False))
    monkeypatch.setattr(client._session, "get", lambda url, params, timeout: seen.update(params=params) or Resp())
    rows = client.get_forecast(40.0, -105.0, days=3)
    assert [r["cloud_cover_pct"] for r in rows] == [10.0, 30.0]  # a missing hour is skipped, not guessed
    assert rows[1]["precipitation_mm"] == 0.2 and rows[0]["time"].endswith("+00:00")
    assert seen["params"]["latitude"] == 40.0 and "cloud_cover" in seen["params"]["hourly"] and seen["params"]["forecast_days"] == 3


def test_a_saved_forecast_stands_in_when_the_live_one_cannot_be_fetched(settings, monkeypatch):
    """The telescope Wi-Fi has no internet. A forecast saved beforehand is a measurement, not an assumption."""
    from dataclasses import replace
    from datetime import datetime, timedelta, timezone

    from tools.weather import WeatherUnavailable, save_forecast

    start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    live = replace(settings, offline_services=False)
    save_forecast(live, [{
        "time": start.isoformat(), "cloud_cover_pct": 90.0, "precipitation_mm": 0.0, "wind_kmh": 1.0,
        "humidity_pct": 40.0, "temperature_c": 8.0, "source": "open-meteo",
    }])
    client = WeatherClient(live)

    def down(*_a, **_k):
        raise WeatherUnavailable("no route")

    monkeypatch.setattr(client, "_fetch", down)
    report = client.get_conditions()
    assert report.source == "saved-forecast" and report.cloud_cover_pct == 90.0
    ok, _reason = _evaluate_weather(report.as_dict(), replace(live, assume_clear_sky=True))
    assert ok is False  # the switch never overrides a forecast that was actually read


def test_a_stale_or_foreign_forecast_is_not_used(settings, monkeypatch):
    from dataclasses import replace
    from datetime import datetime, timedelta, timezone

    import json

    from tools.weather import WeatherUnavailable, forecast_path, save_forecast

    live = replace(settings, offline_services=False, forecast_max_age_h=1.0)
    start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    path = save_forecast(live, [{"time": start.isoformat(), "cloud_cover_pct": 0.0, "wind_kmh": 0.0, "precipitation_mm": 0.0}])
    payload = json.loads(path.read_text())
    payload["fetched_at"] = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    path.write_text(json.dumps(payload))
    client = WeatherClient(live)

    def down(*_a, **_k):
        raise WeatherUnavailable("no route")

    monkeypatch.setattr(client, "_fetch", down)
    with pytest.raises(WeatherUnavailable):
        client.get_conditions()

    payload["fetched_at"] = datetime.now(timezone.utc).isoformat()
    payload["latitude_deg"] = live.site_latitude_deg + 10
    forecast_path(live).write_text(json.dumps(payload))
    with pytest.raises(WeatherUnavailable):
        client.get_conditions()


def test_offline_weather_ignores_a_saved_forecast(settings):
    """Simulation keeps its own cloudy-then-clear sequence; a file on disk must not change it."""
    from datetime import datetime, timezone

    from tools.weather import save_forecast

    start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    save_forecast(settings, [{"time": start.isoformat(), "cloud_cover_pct": 99.0, "wind_kmh": 0, "precipitation_mm": 0}])
    assert WeatherClient(settings).get_conditions().source == "simulated"


def test_dew_closes_a_clear_sky(settings):
    close = {"cloud_cover_pct": 0.0, "wind_kmh": 0.0, "precipitation_mm": 0.0, "temperature_c": 8.0, "humidity_pct": 99.0}
    ok, reason = _evaluate_weather(close, settings)
    assert ok is False and "dew margin" in reason
    open_sky = {**close, "humidity_pct": 40.0}
    assert _evaluate_weather(open_sky, settings)[0] is True


def test_a_forecast_outage_is_an_error_not_a_guess(settings, monkeypatch):
    from dataclasses import replace

    import requests

    client = WeatherClient(replace(settings, offline_services=False))

    def boom(url, params, timeout):
        raise requests.ConnectionError("no route")

    monkeypatch.setattr(client._session, "get", boom)
    with pytest.raises(WeatherUnavailable, match="forecast"):
        client.get_forecast()
