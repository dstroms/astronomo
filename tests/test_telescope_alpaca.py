"""The ASCOM Alpaca transport, against a fake Alpaca server written from the community notes.

Like the JSON-RPC fake, this proves the driver is self-consistent, not that it matches the real
firmware: every behaviour here is UNCONFIRMED until first light.
"""

import json
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np
import pytest
from astropy.io import fits

import tools.seestar_alpaca as alpaca_module
from config import get_settings
from tools.hardware_check import FAIL, INFO, PASS, WARN, run_hardware_check
from tools.seestar_alpaca import (
    AlpacaController,
    parse_ascom_time,
    parse_imagebytes,
    parse_json_image,
    to_uint16,
)
from tools.telescope import SeestarController, TelescopeError, make_controller

TARGET = {"name": "Test Target", "ra_deg": 150.0, "dec_deg": 11.0}
PLAN = {"gain": 80, "exposure_s": 10.0, "filter": "IRCUT", "count": 2}
WIDTH, HEIGHT = 6, 4


def pixel(row: int, col: int) -> int:
    return row * 10 + col  # every pixel value says where it is, so an orientation mistake is visible


def _nested() -> list[list[int]]:
    """ASCOM order: value[x][y] (column index first)."""
    return [[pixel(y, x) for y in range(HEIGHT)] for x in range(WIDTH)]


def _imagebytes(error: int = 0) -> bytes:
    data = np.array(_nested(), dtype="<i4")
    header = struct.pack("<11i", 1, error, 0, 0, 44, 2, 2, 2, WIDTH, HEIGHT, 0)
    return header + data.tobytes()


class _FakeAlpaca(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.calls: list[tuple[str, str, str, dict]] = []
        self.image_mode = "imagebytes"  # "imagebytes" | "json_nested" | "json_flat"
        self.errors: dict[str, tuple[int, str]] = {}  # property/method name -> (ErrorNumber, message)
        self.drop_next: dict[str, int] = {}  # name -> times to cut the connection instead of replying
        self.slew_polls_left = 0
        self.not_ready_polls = 0
        self.equatorial_system = 2
        self.props: dict[str, object] = {
            "name": "Seestar S50", "description": "fake", "driverversion": "1.0", "equatorialsystem": 2,
            "cameraxsize": WIDTH, "cameraysize": HEIGHT, "gainmin": 0, "gainmax": 600, "gain": 80,
            "rightascension": 10.0, "declination": 11.0, "altitude": 45.0, "azimuth": 180.0,
            "tracking": True, "atpark": False, "ccdtemperature": 20.0, "camerastate": 0,
            "sitelatitude": 39.7, "sitelongitude": -105.0,
            "lastexposurestarttime": "2026-10-29T04:00:00.250",
        }

    def names(self, verb: str | None = None) -> list[str]:
        return [n for v, _d, n, _p in self.calls if verb is None or v == verb]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the test output clean
        pass

    def _reply(self, value=None, error=(0, ""), status=200):
        body = json.dumps({"Value": value, "ErrorNumber": error[0], "ErrorMessage": error[1]}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, verb: str):
        url = urlparse(self.path)
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
        if verb == "PUT":
            length = int(self.headers.get("Content-Length", 0))
            params.update({k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()})
        parts = url.path.strip("/").split("/")
        srv: _FakeAlpaca = self.server  # type: ignore[assignment]
        if parts[0] == "management":
            return self._reply([{"DeviceName": "Seestar", "DeviceType": "Telescope"}])
        _api, _v, device, _num, name = parts
        srv.calls.append((verb, device, name, params))
        if srv.drop_next.get(name, 0) > 0:
            srv.drop_next[name] -= 1
            self.connection.close()
            return None
        if name in srv.errors:
            return self._reply(None, srv.errors[name])
        if verb == "PUT":
            if name == "slewtocoordinatesasync":
                srv.slew_polls_left = 1
            return self._reply()
        if name == "slewing":
            polls, srv.slew_polls_left = srv.slew_polls_left, max(0, srv.slew_polls_left - 1)
            return self._reply(polls > 0)
        if name == "imageready":
            if srv.not_ready_polls > 0:
                srv.not_ready_polls -= 1
                return self._reply(False)
            return self._reply(True)
        if name == "imagearray":
            return self._image(srv)
        if name in srv.props:
            return self._reply(srv.props[name])
        return self._reply(None, (1024, f"{name} not implemented"))

    def _image(self, srv: _FakeAlpaca):
        if srv.image_mode == "imagebytes" and "imagebytes" in self.headers.get("Accept", ""):
            body = _imagebytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/imagebytes")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if srv.image_mode == "json_flat":
            return self._reply([pixel(y, x) for y in range(HEIGHT) for x in range(WIDTH)])
        return self._reply(_nested())

    def do_GET(self):  # noqa: N802
        self._handle("GET")

    def do_PUT(self):  # noqa: N802
        self._handle("PUT")


@pytest.fixture()
def fake_alpaca():
    server = _FakeAlpaca()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def _settings(server, tmp_path, **extra):
    return get_settings(
        mock_mode=False, seestar_transport="alpaca", seestar_host="127.0.0.1",
        seestar_alpaca_port=server.server_address[1], seestar_timeout_s=5.0,
        seestar_retries=2, data_dir=tmp_path / "data", **extra,
    )


def _controller(server, tmp_path, slept=None, **extra):
    s = _settings(server, tmp_path, **extra)
    ctl = make_controller(s, sleep_fn=(slept.append if slept is not None else (lambda t: None)))
    assert isinstance(ctl, AlpacaController)
    return ctl


# ---------------------------------------------------------------------------- #
# Choosing the transport
# ---------------------------------------------------------------------------- #
def test_the_factory_follows_the_setting_and_mock_mode_ignores_it(tmp_path):
    assert type(make_controller(get_settings(data_dir=tmp_path))) is SeestarController  # default: jsonrpc
    assert isinstance(make_controller(get_settings(mock_mode=False, seestar_transport="alpaca", data_dir=tmp_path)), AlpacaController)
    mocked = make_controller(get_settings(mock_mode=True, seestar_transport="alpaca", data_dir=tmp_path))
    assert type(mocked) is SeestarController and mocked.mock


def test_an_unknown_transport_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="seestar_transport"):
        get_settings(seestar_transport="carrier-pigeon", data_dir=tmp_path)


def test_the_control_port_follows_the_transport(tmp_path):
    assert get_settings(seestar_transport="alpaca", data_dir=tmp_path).seestar_control_port == 32323
    assert get_settings(seestar_transport="jsonrpc", data_dir=tmp_path).seestar_control_port == 4700


# ---------------------------------------------------------------------------- #
# Connect, slew, configure, capture, park
# ---------------------------------------------------------------------------- #
def test_connect_claims_both_devices_and_pushes_the_site(fake_alpaca, tmp_path):
    ctl = _controller(fake_alpaca, tmp_path)
    with ctl as tel:
        assert tel.connected and tel.firmware is None
        assert tel.device_state["camera_x"] == WIDTH and tel.device_state["telescope_name"] == "Seestar S50"
    puts = {n: p for v, _d, n, p in fake_alpaca.calls if v == "PUT"}
    assert puts["connected"]["Connected"] == "True"
    assert float(puts["sitelatitude"]["SiteLatitude"]) == ctl.settings.site_latitude_deg
    assert float(puts["sitelongitude"]["SiteLongitude"]) == ctl.settings.site_longitude_deg
    assert "utcdate" in puts


def test_site_push_can_be_switched_off(fake_alpaca, tmp_path):
    with _controller(fake_alpaca, tmp_path, seestar_set_site=False):
        pass
    assert not {"sitelatitude", "sitelongitude", "siteelevation", "utcdate"} & set(fake_alpaca.names("PUT"))


def test_setup_refusals_are_logged_not_fatal(fake_alpaca, tmp_path):
    fake_alpaca.errors["utcdate"] = (1036, "not implemented")
    with _controller(fake_alpaca, tmp_path) as tel:
        assert tel.connected


def test_capture_sequence_slews_exposes_and_writes_frames(fake_alpaca, tmp_path):
    with _controller(fake_alpaca, tmp_path) as tel:
        paths = tel.capture_sequence(TARGET, PLAN, tmp_path / "out")
    assert len(paths) == 2
    calls = fake_alpaca.calls
    puts = [(n, p) for v, _d, n, p in calls if v == "PUT"]
    slew = dict(puts)["slewtocoordinatesasync"]
    assert float(slew["RightAscension"]) == 10.0 and float(slew["Declination"]) == 11.0  # RA in hours
    assert dict(puts)["gain"]["Gain"] == "80"
    exposures = [p for n, p in puts if n == "startexposure"]
    assert len(exposures) == 2 and exposures[0]["Duration"] == "10.0" and exposures[0]["Light"] == "True"
    names = fake_alpaca.names()
    assert names.index("unpark") < names.index("slewtocoordinatesasync") < names.index("startexposure")
    assert names.count("unpark") == 1  # once per connection, not per frame
    with fits.open(paths[0]) as hdul:
        data, hdr = hdul[0].data, hdul[0].header
        assert data.shape == (HEIGHT, WIDTH) and data.dtype == np.uint16
        assert int(data[1, 2]) == pixel(1, 2) and int(data[3, 5]) == pixel(3, 5)  # [row, column], not transposed
        assert hdr["OBJECT"] == "Test Target" and hdr["RA"] == 150.0 and hdr["EXPTIME"] == 10.0 and hdr["GAIN"] == 80
        assert hdr["DATE-OBS"] == "2026-10-29T04:00:00.250" and hdr["TIMESRC"] == "device"
        assert "FILTER" not in hdr  # no filter was selected, so none is claimed


@pytest.mark.parametrize("mode", ["imagebytes", "json_nested", "json_flat"])
def test_every_image_encoding_gives_the_same_frame(fake_alpaca, tmp_path, mode):
    fake_alpaca.image_mode = mode
    with _controller(fake_alpaca, tmp_path) as tel:
        paths = tel.capture_sequence(TARGET, {**PLAN, "count": 1}, tmp_path / "out")
    with fits.open(paths[0]) as hdul:
        expected = np.array([[pixel(r, c) for c in range(WIDTH)] for r in range(HEIGHT)])
        assert np.array_equal(hdul[0].data, expected)


def test_the_host_clock_is_used_and_labelled_when_the_camera_gives_no_start_time(fake_alpaca, tmp_path):
    del fake_alpaca.props["lastexposurestarttime"]
    with _controller(fake_alpaca, tmp_path) as tel:
        paths = tel.capture_sequence(TARGET, {**PLAN, "count": 1}, tmp_path / "out")
    with fits.open(paths[0]) as hdul:
        assert hdul[0].header["TIMESRC"] == "host clock" and hdul[0].header["DATE-OBS"].startswith("20")


def test_no_image_share_is_needed(fake_alpaca, tmp_path):
    with _controller(fake_alpaca, tmp_path, seestar_share_path=None) as tel:
        assert len(tel.capture_sequence(TARGET, {**PLAN, "count": 1}, tmp_path / "out")) == 1


def test_slew_waits_for_the_mount_to_stop_moving(fake_alpaca, tmp_path):
    fake_alpaca.slew_polls_left = 0
    slept = []
    with _controller(fake_alpaca, tmp_path, slept) as tel:
        tel.slew_to(TARGET)
    assert fake_alpaca.names().count("slewing") == 2  # moving, then stopped
    assert 2.0 in slept


def test_a_slow_camera_is_polled_until_the_image_is_ready(fake_alpaca, tmp_path):
    fake_alpaca.not_ready_polls = 3
    with _controller(fake_alpaca, tmp_path) as tel:
        tel.capture_sequence(TARGET, {**PLAN, "count": 1}, tmp_path / "out")
    assert fake_alpaca.names().count("imageready") == 4


def test_a_camera_that_never_finishes_fails_clearly(fake_alpaca, tmp_path, monkeypatch):
    monkeypatch.setattr(alpaca_module, "READOUT_ALLOWANCE_S", 0.05)
    fake_alpaca.not_ready_polls = 10**6
    with pytest.raises(TelescopeError, match="did not report an image ready"):
        with _controller(fake_alpaca, tmp_path) as tel:
            tel.capture_sequence(TARGET, {**PLAN, "count": 1, "exposure_s": 0.0}, tmp_path / "out")


def test_park_and_failure_leaves_the_mount_safe(fake_alpaca, tmp_path):
    fake_alpaca.errors["startexposure"] = (1031, "camera busy")
    with pytest.raises(TelescopeError, match="camera busy"):
        with _controller(fake_alpaca, tmp_path) as tel:
            tel.capture_sequence(TARGET, PLAN, tmp_path / "out")
    assert "park" in fake_alpaca.names("PUT")


# ---------------------------------------------------------------------------- #
# Errors and retries
# ---------------------------------------------------------------------------- #
def test_device_errors_carry_their_number_and_are_never_retried(fake_alpaca, tmp_path):
    fake_alpaca.errors["slewtocoordinatesasync"] = (1032, "not initialised: unpark first")
    with pytest.raises(TelescopeError, match="unpark first") as err:
        with _controller(fake_alpaca, tmp_path) as tel:
            tel.slew_to(TARGET)
    assert err.value.code == 1032 and not err.value.transient
    assert fake_alpaca.names().count("slewtocoordinatesasync") == 1


def test_a_dropped_connection_is_retried_with_backoff(fake_alpaca, tmp_path):
    fake_alpaca.drop_next = {"gain": 1}
    slept = []
    with _controller(fake_alpaca, tmp_path, slept) as tel:
        tel.configure(PLAN)
    assert fake_alpaca.names("PUT").count("gain") == 2  # the cut one, then the retry
    assert 2.0 in slept


def test_it_gives_up_after_the_configured_retries(fake_alpaca, tmp_path):
    fake_alpaca.drop_next = {"gain": 99}
    slept = []
    with pytest.raises(TelescopeError) as err:
        with _controller(fake_alpaca, tmp_path, slept) as tel:
            tel.configure(PLAN)
    assert err.value.transient and fake_alpaca.names("PUT").count("gain") == 3  # first try + 2 retries
    assert [x for x in slept if x >= 2.0][:2] == [2.0, 4.0]


def test_an_unreachable_scope_is_a_clear_error(tmp_path):
    s = get_settings(mock_mode=False, seestar_transport="alpaca", seestar_host="127.0.0.1", seestar_alpaca_port=1,
                     seestar_timeout_s=1.0, seestar_retries=0, data_dir=tmp_path)
    with pytest.raises(TelescopeError, match="Connection|refused|connect"):
        make_controller(s).connect()


def test_a_refused_connection_names_the_things_to_check(fake_alpaca, tmp_path):
    fake_alpaca.errors["connected"] = (1035, "app is connected")
    with pytest.raises(TelescopeError, match="Alpaca switch"):
        _controller(fake_alpaca, tmp_path).connect()


def test_there_is_no_solver_so_pointing_is_never_called_verified(fake_alpaca, tmp_path):
    with _controller(fake_alpaca, tmp_path) as tel:
        assert tel.solve_pointing() is None


def test_json_rpc_queries_are_not_available(fake_alpaca, tmp_path):
    with _controller(fake_alpaca, tmp_path) as tel:
        with pytest.raises(TelescopeError, match="JSON-RPC"):
            tel.query("get_coord")


def test_a_mount_in_another_equatorial_system_is_flagged_in_the_log(fake_alpaca, tmp_path, caplog):
    fake_alpaca.props["equatorialsystem"] = 1
    with _controller(fake_alpaca, tmp_path):
        pass
    assert "not J2000" in caplog.text


# ---------------------------------------------------------------------------- #
# Hardware check over Alpaca
# ---------------------------------------------------------------------------- #
def _by_name(report):
    return {s["name"]: s for s in report["steps"]}


def _with_site(settings):
    from dataclasses import replace

    return replace(settings, site_source="file")


def test_the_smoke_test_passes_over_alpaca_and_moves_nothing(fake_alpaca, tmp_path):
    settings = _with_site(_settings(fake_alpaca, tmp_path))
    fake_alpaca.props["sitelatitude"] = settings.site_latitude_deg
    fake_alpaca.props["sitelongitude"] = settings.site_longitude_deg
    report = run_hardware_check(settings)
    steps = _by_name(report)
    assert report["ok"], report
    assert steps["connect"]["status"] == PASS and "alpaca" in steps["connect"]["detail"]
    assert steps["firmware"]["status"] == INFO and steps["coordinate system"]["status"] == PASS
    assert steps["location matches site"]["status"] == PASS and steps["park"]["status"] == PASS
    assert "raw" in steps["position"] and "raw" in steps["camera"]
    assert steps["share"]["status"] == INFO
    puts = fake_alpaca.names("PUT")
    assert "park" in puts
    assert not {"slewtocoordinatesasync", "startexposure", "gain", "unpark"} & set(puts)


def test_the_smoke_test_warns_about_a_non_j2000_mount(fake_alpaca, tmp_path):
    fake_alpaca.props["equatorialsystem"] = 1
    steps = _by_name(run_hardware_check(_with_site(_settings(fake_alpaca, tmp_path))))
    assert steps["coordinate system"]["status"] == WARN


def test_the_smoke_test_fails_helpfully_when_alpaca_is_unreachable(tmp_path):
    s = get_settings(mock_mode=False, seestar_transport="alpaca", seestar_host="127.0.0.1", seestar_alpaca_port=1,
                     seestar_retries=0, seestar_timeout_s=1.0, data_dir=tmp_path)
    report = run_hardware_check(s)
    assert not report["ok"] and _by_name(report)["connect"]["status"] == FAIL


def test_the_json_rpc_failure_hint_points_at_alpaca(tmp_path):
    s = get_settings(mock_mode=False, seestar_host="127.0.0.1", seestar_port=1, seestar_retries=0, data_dir=tmp_path)
    detail = _by_name(run_hardware_check(s))["connect"]["detail"]
    assert "ASTRONOMO_SEESTAR_TRANSPORT=alpaca" in detail


# ---------------------------------------------------------------------------- #
# Decoding
# ---------------------------------------------------------------------------- #
def test_imagebytes_decodes_to_row_column_order():
    arr = parse_imagebytes(_imagebytes())
    assert arr.shape == (HEIGHT, WIDTH) and int(arr[2, 4]) == pixel(2, 4)


def test_imagebytes_uint16_is_element_type_8():
    data = np.array(_nested(), dtype="<u2")
    header = struct.pack("<11i", 1, 0, 0, 0, 44, 8, 8, 2, WIDTH, HEIGHT, 0)
    arr = parse_imagebytes(header + data.tobytes())
    assert arr.shape == (HEIGHT, WIDTH) and arr.dtype == np.uint16 and int(arr[2, 4]) == pixel(2, 4)


def test_imagebytes_reports_a_device_error():
    with pytest.raises(TelescopeError, match="1280"):
        parse_imagebytes(_imagebytes(error=1280))


def test_imagebytes_rejects_garbage():
    with pytest.raises(TelescopeError):
        parse_imagebytes(b"short")
    with pytest.raises(TelescopeError, match="does not match"):
        parse_imagebytes(_imagebytes()[:-8])


def test_a_flat_array_of_the_wrong_size_is_refused():
    with pytest.raises(TelescopeError, match="cannot interpret"):
        parse_json_image([1, 2, 3], WIDTH, HEIGHT)


def test_pixel_values_are_clipped_into_unsigned_16_bit():
    out = to_uint16(np.array([[-5, 70000], [12.4, 12.6]]))
    assert out.dtype == np.uint16 and out.tolist() == [[0, 65535], [12, 13]]


def test_ascom_times_parse_as_utc():
    t = parse_ascom_time("2026-10-29T04:00:00.250")
    assert t is not None and t.tzinfo is not None and t.microsecond == 250000
    assert parse_ascom_time("") is None and parse_ascom_time("garbage") is None and parse_ascom_time(None) is None
