"""Mock capture + the real-hardware JSON-RPC path against a fake Seestar TCP server."""

import json
import socketserver
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from config import get_settings
from tools.telescope import SeestarController, TelescopeError

TARGET = {"name": "Test Target", "ra_deg": 150.0, "dec_deg": 11.0, "sim": {"epoch0_iso": "2026-10-21T04:00:00+00:00"}}
PLAN = {"gain": 80, "exposure_s": 10.0, "filter": "IRCUT", "count": 2}


def test_mock_capture_writes_fits_with_wcs_and_simulated_flag(settings, tmp_path):
    from dataclasses import replace

    slept = []
    settings = replace(settings, mock_capture_seconds=10.0)  # the brief's 10 s simulated capture
    with SeestarController(settings, sleep_fn=slept.append) as tel:
        paths = tel.capture_sequence(TARGET, PLAN, tmp_path)
    assert len(paths) == 2
    with fits.open(paths[0]) as hdul:
        assert hdul[0].data.shape == (1080, 1920) and hdul[0].data.dtype == np.uint16
        hdr = hdul[0].header
        assert hdr["SIMULATD"] is True and hdr["OBJECT"] == "Test Target"
        assert hdr["CTYPE1"] == "RA---TAN" and hdr["EXPTIME"] == 10.0
    assert slept.count(10.0) == 2  # exactly one 10 s simulated capture per frame


def test_after_frame_stops_the_sequence_before_the_next_exposure(settings, tmp_path):
    from dataclasses import replace

    slept = []
    seen = []

    def stop(index, _path):
        seen.append(index)
        if index == 0:
            raise RuntimeError("bad field")

    settings = replace(settings, mock_capture_seconds=0.0)
    with SeestarController(settings, sleep_fn=slept.append) as tel:
        with pytest.raises(RuntimeError, match="bad field"):
            tel.capture_sequence(TARGET, {**PLAN, "count": 4}, tmp_path, after_frame=stop)
    assert seen == [0]
    assert len(list(Path(tmp_path).glob("*.fits"))) == 1
    assert 10.0 not in slept  # the stop happened before any real exposure wait


def test_mock_capture_default_duration_is_ten_seconds():
    assert get_settings().mock_capture_seconds == 10.0


def test_mock_mode_never_opens_a_socket(settings, tmp_path, monkeypatch):
    import socket

    def boom(*a, **k):
        raise AssertionError("socket opened in mock mode")

    monkeypatch.setattr(socket, "create_connection", boom)
    with SeestarController(settings, sleep_fn=lambda s: None) as tel:
        tel.capture_sequence(TARGET, PLAN, tmp_path)


def test_capture_requires_connect(settings, tmp_path):
    with pytest.raises(TelescopeError):
        SeestarController(settings).capture_sequence(TARGET, PLAN, tmp_path)


# ---------------------------------------------------------------------------- #
# Fake Seestar
# ---------------------------------------------------------------------------- #
class _FakeSeestar(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, share: Path, fail_method: str | None = None):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.share, self.fail_method = share, fail_method
        self.calls: list[tuple[str, object]] = []
        self.device_state: object = {"device": {"name": "Seestar S50", "firmware_ver_int": 2400}}
        self.require_verify = False  # refuse any request that lacks the "verify" flag
        self.refuse_all = False  # refuse everything (as firmware wanting an auth handshake might)
        self.drop_next: dict[str, int] = {}  # method -> times to cut the connection instead of replying
        self.solve_reply: object = {"ra_dec": [10.0, 11.0]}


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        for line in self.rfile:
            req = json.loads(line)
            method, params = req["method"], req.get("params")
            self.server.calls.append((method, params))
            if self.server.drop_next.get(method, 0) > 0:
                self.server.drop_next[method] -= 1
                return  # close the connection mid-request
            # An unsolicited event first, plus a stale reply: the client must skip both.
            self.wfile.write(b'{"Event":"PiStatus","temp":30}\r\n{"id":1,"code":0,"result":"stale"}\r\n')
            has_verify = "verify" in json.dumps(params)
            if self.server.refuse_all or (self.server.require_verify and not has_verify):
                reply = {"id": req["id"], "code": 109, "error": "verify required"}
            elif method == self.server.fail_method:
                reply = {"id": req["id"], "code": 207, "error": "fail to operate"}
            elif method == "get_device_state":
                reply = {"id": req["id"], "code": 0, "result": self.server.device_state}
            elif method == "get_solve_result":
                reply = {"id": req["id"], "code": 0, "result": self.server.solve_reply}
            elif method == "scope_get_equ_coord":
                reply = {"id": req["id"], "code": 0, "result": {"ra": 10.0, "dec": 11.0}}  # 150 deg
            else:
                reply = {"id": req["id"], "code": 0, "result": 0}
            if method == "iscope_start_stack":
                data = np.full((20, 20), 5, dtype=np.uint16)
                fits.PrimaryHDU(data).writeto(self.server.share / f"stack_{time.time_ns()}.fit")
            self.wfile.write((json.dumps(reply) + "\r\n").encode())


@pytest.fixture()
def fake_seestar(tmp_path):
    share = tmp_path / "share"
    share.mkdir()
    server = _FakeSeestar(share)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def _real_settings(server, share):
    return get_settings(
        mock_mode=False,
        seestar_host="127.0.0.1",
        seestar_port=server.server_address[1],
        seestar_timeout_s=5.0,
        seestar_share_path=str(share),
        data_dir=share.parent / "data",
    )


def test_real_mode_protocol_slew_configure_capture(fake_seestar, tmp_path):
    s = _real_settings(fake_seestar, fake_seestar.share)
    with SeestarController(s, sleep_fn=lambda t: None) as tel:
        paths = tel.capture_sequence(TARGET, {**PLAN, "count": 1}, tmp_path / "out")
    assert len(paths) == 1 and Path(paths[0]).is_file()
    methods = [m for m, _ in fake_seestar.calls]
    after_setup = methods[methods.index("iscope_start_view") - 1 :]
    assert after_setup[:3] == ["scope_move_to_horizon", "iscope_start_view", "scope_get_equ_coord"]
    assert "iscope_start_stack" in methods and "iscope_stop_view" in methods
    start_view = dict(fake_seestar.calls)["iscope_start_view"]
    assert start_view["target_ra_dec"] == [10.0, 11.0]  # RA converted to hours
    with fits.open(paths[0]) as hdul:  # headers patched so downstream code can rely on them
        assert hdul[0].header["OBJECT"] == "Test Target" and hdul[0].header["RA"] == 150.0


def test_real_mode_surfaces_device_errors_and_parks(fake_seestar, tmp_path):
    fake_seestar.fail_method = "iscope_start_stack"
    s = _real_settings(fake_seestar, fake_seestar.share)
    with pytest.raises(TelescopeError, match="fail to operate"):
        with SeestarController(s, sleep_fn=lambda t: None) as tel:
            tel.capture_sequence(TARGET, PLAN, tmp_path / "out")
    assert "scope_park" in [m for m, _ in fake_seestar.calls]  # left safe after failure


def test_real_mode_needs_share_path(fake_seestar, tmp_path):
    s = get_settings(
        mock_mode=False, seestar_host="127.0.0.1", seestar_port=fake_seestar.server_address[1],
        seestar_share_path=None, data_dir=tmp_path,
    )
    with pytest.raises(TelescopeError, match="SHARE_PATH"):
        with SeestarController(s, sleep_fn=lambda t: None) as tel:
            tel.capture_sequence(TARGET, PLAN, tmp_path / "out")


def test_real_mode_connection_refused_is_a_clear_error(tmp_path):
    s = get_settings(mock_mode=False, seestar_host="127.0.0.1", seestar_port=1, seestar_timeout_s=1.0, data_dir=tmp_path)
    slept = []
    with pytest.raises(TelescopeError, match="cannot connect"):
        SeestarController(s, sleep_fn=slept.append).connect()
    assert slept == [2.0, 4.0, 8.0]  # three retries, exponential backoff


# ---------------------------------------------------------------------------- #
# Connection set-up, firmware, retries, heartbeat (all against the fake; the real
# scope's behaviour is unconfirmed until first light)
# ---------------------------------------------------------------------------- #
def _methods(server):
    return [m for m, _ in server.calls]


def test_connect_sets_up_the_session_from_the_site_profile(fake_seestar):
    s = _real_settings(fake_seestar, fake_seestar.share)
    with SeestarController(s, sleep_fn=lambda t: None) as tel:
        assert tel.firmware == 2400
    calls = dict(fake_seestar.calls)
    assert _methods(fake_seestar)[0] == "get_device_state"
    assert calls["set_user_location"]["lat"] == s.site_latitude_deg
    assert calls["set_user_location"]["lon"] == s.site_longitude_deg
    assert calls["pi_set_time"][0]["time_zone"] == "UTC"
    assert {"master_cli": True} in [p for m, p in fake_seestar.calls if m == "set_setting"]
    assert calls["set_stack_setting"] == {"save_discrete_frame": True}


def test_site_push_can_be_switched_off(fake_seestar):
    s = get_settings(**{**_real_settings(fake_seestar, fake_seestar.share).__dict__, "seestar_set_site": False})
    with SeestarController(s, sleep_fn=lambda t: None):
        pass
    assert "set_user_location" not in _methods(fake_seestar) and "pi_set_time" not in _methods(fake_seestar)


def test_setup_refusals_are_logged_not_fatal(fake_seestar):
    fake_seestar.fail_method = "set_user_location"
    s = _real_settings(fake_seestar, fake_seestar.share)
    with SeestarController(s, sleep_fn=lambda t: None) as tel:
        assert tel.connected


def test_verify_is_injected_only_in_the_firmware_window(fake_seestar):
    fake_seestar.device_state = {"device": {"firmware_ver_int": 2600}}
    s = _real_settings(fake_seestar, fake_seestar.share)
    with SeestarController(s, sleep_fn=lambda t: None):
        pass
    after_state = [p for m, p in fake_seestar.calls if m == "set_stack_setting"]
    assert after_state == [{"save_discrete_frame": True, "verify": True}]


def test_verify_is_not_injected_on_other_firmware(fake_seestar):
    fake_seestar.device_state = {"device": {"firmware_ver_int": 2706}}
    s = _real_settings(fake_seestar, fake_seestar.share)
    with SeestarController(s, sleep_fn=lambda t: None):
        pass
    assert all("verify" not in json.dumps(p) for _, p in fake_seestar.calls)


def test_a_scope_that_demands_verify_is_handled_even_if_the_version_is_unreadable(fake_seestar):
    fake_seestar.device_state = {"device": {}}  # no firmware key
    fake_seestar.require_verify = True
    s = _real_settings(fake_seestar, fake_seestar.share)
    with SeestarController(s, sleep_fn=lambda t: None) as tel:
        assert tel.firmware is None
        assert tel._rpc.inject_verify is True


def test_a_scope_that_refuses_everything_fails_with_the_handshake_hint(fake_seestar):
    fake_seestar.refuse_all = True
    s = _real_settings(fake_seestar, fake_seestar.share)
    with pytest.raises(TelescopeError, match="7.18"):
        SeestarController(s, sleep_fn=lambda t: None).connect()


def test_dropped_connection_is_reopened_set_up_again_and_retried(fake_seestar, tmp_path):
    fake_seestar.drop_next = {"iscope_start_view": 1}
    s = _real_settings(fake_seestar, fake_seestar.share)
    slept = []
    with SeestarController(s, sleep_fn=slept.append) as tel:
        paths = tel.capture_sequence(TARGET, {**PLAN, "count": 1}, tmp_path / "out")
        assert tel._rpc.reconnects == 1
    assert len(paths) == 1
    assert _methods(fake_seestar).count("get_device_state") == 2  # set-up replayed after the reconnect
    assert _methods(fake_seestar).count("iscope_start_view") == 2  # the cut one, then the retry
    assert 2.0 in slept  # the first backoff


def test_gives_up_after_the_configured_retries(fake_seestar, tmp_path):
    fake_seestar.drop_next = {"iscope_start_view": 99}
    s = _real_settings(fake_seestar, fake_seestar.share)
    slept = []
    with pytest.raises(TelescopeError, match="connection closed"):
        with SeestarController(s, sleep_fn=slept.append) as tel:
            tel.capture_sequence(TARGET, PLAN, tmp_path / "out")
    assert _methods(fake_seestar).count("iscope_start_view") == 4  # first try + 3 retries
    assert [x for x in slept if x >= 2.0][:3] == [2.0, 4.0, 8.0]  # (then one more for the parking call)


def test_device_errors_are_never_retried(fake_seestar, tmp_path):
    fake_seestar.fail_method = "iscope_start_view"
    s = _real_settings(fake_seestar, fake_seestar.share)
    with pytest.raises(TelescopeError, match="fail to operate") as err:
        with SeestarController(s, sleep_fn=lambda t: None) as tel:
            tel.capture_sequence(TARGET, PLAN, tmp_path / "out")
    assert err.value.code == 207 and not err.value.transient
    assert _methods(fake_seestar).count("iscope_start_view") == 1


def test_long_waits_send_a_heartbeat(fake_seestar):
    s = _real_settings(fake_seestar, fake_seestar.share)
    slept = []
    with SeestarController(s, sleep_fn=slept.append) as tel:
        before = _methods(fake_seestar).count("scope_get_equ_coord")
        tel._idle(30.0)
        pings = _methods(fake_seestar).count("scope_get_equ_coord") - before
    assert sum(slept) == pytest.approx(30.0) and pings == 3  # 8 + 8 + 8 + 6: pings between slices


def test_raw_sub_frames_are_preferred_over_the_live_stack(fake_seestar, tmp_path):
    s = _real_settings(fake_seestar, fake_seestar.share)
    stack_dir = fake_seestar.share / "Target"
    sub_dir = fake_seestar.share / "Target_sub"
    stack_dir.mkdir()
    sub_dir.mkdir()
    for d, value in ((sub_dir, 7), (stack_dir, 9)):  # the stack is written last, so it is "newest"
        fits.PrimaryHDU(np.full((8, 8), value, dtype=np.uint16)).writeto(d / "x.fit")
        time.sleep(0.02)
    with SeestarController(s, sleep_fn=lambda t: None) as tel:
        out = tel._collect_fits(fake_seestar.share, 0.0, tmp_path / "f.fits", TARGET, PLAN)
    with fits.open(out) as hdul:
        assert int(hdul[0].data[0, 0]) == 7


def test_solve_pointing_reads_the_scopes_own_solution(fake_seestar):
    s = _real_settings(fake_seestar, fake_seestar.share)
    with SeestarController(s, sleep_fn=lambda t: None) as tel:
        assert tel.solve_pointing() == {"ra_deg": 150.0, "dec_deg": 11.0}


def test_solve_pointing_unreadable_reply_is_not_verified(fake_seestar):
    fake_seestar.solve_reply = {"something": "else"}
    s = _real_settings(fake_seestar, fake_seestar.share)
    with SeestarController(s, sleep_fn=lambda t: None) as tel:
        assert tel.solve_pointing(timeout_s=0.0) is None


def test_firmware_is_found_in_nested_replies():
    from tools.telescope import find_firmware_int

    assert find_firmware_int({"a": {"b": {"fw_ver_int": "2706"}}}) == 2706
    assert find_firmware_int({"nothing": 1}) is None
