"""Autofocus: the curve fit, the sweep logic, the Alpaca focuser wrapper, and a simulated run.

Everything here uses a fake focuser or the simulated sky.  Nothing has run on a physical Seestar, so none of this
says how the real focuser behaves; it says the routine does what it claims against a focuser that follows the ASCOM
Focuser specification.
"""

import math

import numpy as np
import pytest

from tools import autofocus as af
from tools.telescope import TelescopeError

# The Alpaca device list the S50 Pro returned on 2026-11-13 (the Wide Angle devices are number 1).
CONFIGURED = [
    {"DeviceName": "Seestar S50 Pro_ed666d24 Wide Angle Camera", "DeviceType": "Camera", "DeviceNumber": 1},
    {"DeviceName": "Seestar S50 Pro_ed666d24 Wide Angle Focuser", "DeviceType": "Focuser", "DeviceNumber": 1},
    {"DeviceName": "Seestar S50 Pro_ed666d24 Telephoto Focuser", "DeviceType": "Focuser", "DeviceNumber": 0},
]


class FakeFocuser:
    """A focuser whose star width is a hyperbola about a hidden best position."""

    def __init__(self, best, start, maxstep=2600, min_fwhm=3.0, slope=0.03, stars=True, fail_after=None):
        self.best, self.position, self.maxstep = best, start, maxstep
        self.min_fwhm, self.slope, self.stars, self.fail_after = min_fwhm, slope, stars, fail_after
        self.moves, self.halted = [], 0

    def info(self):
        return {"position": self.position, "maxstep": self.maxstep, "absolute": True}

    def move_to(self, position):
        assert 0 <= position <= self.maxstep, f"moved outside the range: {position}"
        self.moves.append(position)
        self.position = position
        return position

    def halt(self):
        self.halted += 1

    def frame(self, exposure_s, gain):
        if self.fail_after is not None and len(self.moves) > self.fail_after:
            raise TelescopeError("the camera stopped answering")
        return np.full((4, 4), float(self.position))  # the position travels in the "image"

    def width(self, position):
        return math.hypot(self.min_fwhm, self.slope * (position - self.best))


def measure_for(dev):
    def measure(frame):
        if not dev.stars:
            return None
        return {"fwhm_px": dev.width(int(frame[0, 0])), "n_stars": 30}
    return measure


def run(dev, **kw):
    kw.setdefault("sleep", lambda s: None)
    return af.run_autofocus(dev, measure_for(dev), **kw)


# --------------------------------------------------------------------------- #
# The curve
# --------------------------------------------------------------------------- #
def test_the_fit_finds_the_minimum_of_a_hyperbola():
    dev = FakeFocuser(best=1234, start=1000)
    samples = [{"position": p, "fwhm_px": dev.width(p)} for p in range(1000, 1500, 80)]
    fit = af.fit_focus_curve(samples)
    assert fit["inside"] and abs(fit["position"] - 1234) <= 8 and fit["fwhm_px"] == pytest.approx(3.0, abs=0.3)


def test_a_minimum_outside_the_window_is_not_trusted():
    dev = FakeFocuser(best=2000, start=1000)
    samples = [{"position": p, "fwhm_px": dev.width(p)} for p in range(900, 1300, 60)]
    fit = af.fit_focus_curve(samples)
    assert fit is None or not fit["inside"]


def test_too_few_measured_stars_give_no_fit():
    assert af.fit_focus_curve([{"position": p, "fwhm_px": 5.0} for p in (1, 2, 3)]) is None
    assert af.fit_focus_curve([{"position": p, "fwhm_px": None} for p in range(10)]) is None


def test_the_window_is_clamped_to_the_range():
    pos = af.window(50, 200, 7, 0, 2600)
    assert pos == sorted(set(pos)) and min(pos) == 0 and max(pos) == 250


# --------------------------------------------------------------------------- #
# The sweep
# --------------------------------------------------------------------------- #
def test_a_defocused_scope_is_brought_to_focus():
    dev = FakeFocuser(best=1180, start=1000)
    report = run(dev, half_width=150)
    assert report["status"] == af.IMPROVED
    assert abs(dev.position - 1180) <= 15
    assert report["fwhm_after_px"] < 0.5 * report["fwhm_before_px"]
    assert report["final_position"] == dev.position


def test_the_sweep_widens_when_the_best_focus_is_outside_the_first_window():
    dev = FakeFocuser(best=1500, start=1000)
    report = run(dev, half_width=100, max_widenings=3)
    assert report["status"] == af.IMPROVED and abs(dev.position - 1500) <= 30


def test_the_fit_gives_the_position_where_the_star_reaches_a_target_width():
    dev = FakeFocuser(best=1180, start=1000, min_fwhm=2.2)
    fit = af.fit_focus_curve([{"position": p, "fwhm_px": dev.width(p)} for p in range(1000, 1400, 60)])
    where = af.position_for_width(fit, 3.5, 0, 2600)
    assert where is not None and where < fit["position"], "the low side, which is the side the sweep approaches from"
    assert dev.width(where) == pytest.approx(3.5, abs=0.15)
    assert af.position_for_width(fit, 2.0, 0, 2600) is None, "a target sharper than the best focus is not reachable"
    assert af.position_for_width(fit, 3.5, 1150, 2600) == 1150, "held to the range"


def test_autofocus_settles_at_the_target_width_not_at_the_sharpest_point():
    """At 2.2 px a Seestar star is a few bright pixels, and photometry depends on where the star falls."""
    dev = FakeFocuser(best=1180, start=1000, min_fwhm=2.2)
    report = run(dev, half_width=150, target_fwhm_px=3.5)
    assert report["status"] == af.IMPROVED, report
    assert report["sharpest_position"] == pytest.approx(1180, abs=15) and report["target_fwhm_px"] == 3.5
    assert dev.position < report["sharpest_position"] - 50, "stopped short of the sharpest focus on purpose"
    assert report["fwhm_after_px"] == pytest.approx(3.5, abs=0.25)
    assert dev.moves[-1] > dev.moves[-2], "still approached from below"


def test_a_focus_target_can_soften_a_scope_that_starts_too_sharp():
    dev = FakeFocuser(best=1000, start=1000, min_fwhm=2.2)  # the sharp state the 2026-11-16 sweep ended in
    report = run(dev, half_width=200, target_fwhm_px=3.5)
    assert report["status"] == af.IMPROVED and dev.position < 1000
    assert abs(report["fwhm_after_px"] - 3.5) < 0.3 < abs(report["fwhm_before_px"] - 3.5)


def test_a_scope_already_at_the_target_width_is_left_where_it_is():
    dev = FakeFocuser(best=1180, start=1090, min_fwhm=2.2)  # about 3.5 px already
    report = run(dev, half_width=150, target_fwhm_px=3.5)
    assert report["status"] == af.NO_IMPROVEMENT and dev.position == 1090


def test_a_target_wider_than_the_best_focus_changes_nothing():
    """If the best the scope can do is soft, the target is moot: take the best."""
    dev = FakeFocuser(best=1180, start=1000, min_fwhm=4.0)
    report = run(dev, half_width=150, target_fwhm_px=3.5)
    assert report["status"] == af.IMPROVED and "target_fwhm_px" not in report
    assert abs(dev.position - 1180) <= 15


def test_zero_target_takes_the_sharpest_focus():
    dev = FakeFocuser(best=1180, start=1000, min_fwhm=2.2)
    assert run(dev, half_width=150, target_fwhm_px=0.0)["status"] == af.IMPROVED
    assert abs(dev.position - 1180) <= 15


def test_the_target_is_a_setting_and_a_command_line_flag(settings):
    import main

    assert settings.focus_target_fwhm_px == 0.0  # off until it has been compared on real frames
    args = main.parse_args(["--focus-step", "--focus-target", "3.5"])
    assert main.build_settings(args).focus_target_fwhm_px == 3.5
    assert main.build_settings(main.parse_args(["--focus-step"])).focus_target_fwhm_px == 0.0


def test_a_scope_already_in_focus_is_left_where_it_is():
    dev = FakeFocuser(best=1000, start=1000)
    report = run(dev, half_width=150)
    assert report["status"] == af.NO_IMPROVEMENT and dev.position == 1000


def test_the_final_approach_is_from_below():
    dev = FakeFocuser(best=1180, start=1000)
    run(dev, half_width=150)
    assert dev.moves[-1] > dev.moves[-2], "the last move should climb to the position, as the sweep did"


def test_no_stars_anywhere_puts_the_focuser_back():
    dev = FakeFocuser(best=1180, start=1000, stars=False)
    report = run(dev, half_width=150)
    assert report["status"] == af.NO_STARS and dev.position == 1000


def test_no_minimum_in_the_range_puts_the_focuser_back():
    dev = FakeFocuser(best=5000, start=1000, maxstep=2600)  # the real best is beyond the end of the range
    report = run(dev, half_width=200, max_widenings=2)
    assert report["status"] in (af.NO_MINIMUM, af.NO_IMPROVEMENT) and dev.position == 1000


def test_a_failure_mid_sweep_returns_the_focuser_to_the_start_and_re_raises():
    dev = FakeFocuser(best=1180, start=1000, fail_after=4)
    with pytest.raises(TelescopeError):
        run(dev, half_width=150)
    assert dev.position == 1000 and dev.halted == 0, "Halt is a last resort: a Halt then Move was refused on the real scope"


class StubbornFocuser(FakeFocuser):
    """Refuses the first ``refusals`` moves made after the camera fails (or all of them if ``refusals`` is None)."""

    def __init__(self, *a, refusals=1, **kw):
        super().__init__(*a, fail_after=3, **kw)
        self.refusals = refusals

    def move_to(self, position):
        if len(self.moves) > 3 and (self.refusals is None or self.refusals > 0):
            if self.refusals is not None:
                self.refusals -= 1
            raise TelescopeError("SET_MOVE_FOCUSER fail: internal error (1279)")
        return super().move_to(position)


def test_a_refused_return_is_retried_and_reported_as_restored():
    dev = StubbornFocuser(best=1180, start=1000, refusals=1)
    with pytest.raises(TelescopeError, match="camera") as caught:
        run(dev, half_width=150)
    assert dev.position == 1000
    assert caught.value.focuser_restored is True and caught.value.focuser_start == 1000


def test_a_focuser_that_cannot_be_returned_is_reported_loudly(caplog):
    dev = StubbornFocuser(best=1180, start=1000, refusals=None)
    with caplog.at_level("ERROR"), pytest.raises(TelescopeError) as caught:
        run(dev, half_width=150)
    assert caught.value.focuser_restored is False and caught.value.focuser_start == 1000
    assert dev.halted >= 1, "Halt is tried once the plain moves have failed"
    assert "NOT RETURNED" in caplog.text and "started at 1000" in caplog.text


def test_a_stop_request_returns_the_focuser_and_stops():
    from tools.telescope import StopRequested

    dev = FakeFocuser(best=1180, start=1000)
    with pytest.raises(StopRequested) as caught:
        run(dev, half_width=150, stop_check=lambda: len(dev.moves) >= 3)
    assert dev.position == 1000 and caught.value.focuser_restored is True


def test_the_sweep_logs_the_focuser_and_every_position(caplog):
    dev = FakeFocuser(best=1180, start=1000)
    with caplog.at_level("INFO"):
        run(dev, half_width=150)
    assert "Focus: focuser" in caplog.text and "maxstep" in caplog.text
    assert caplog.text.count("Focus: position") >= 7


def test_an_interrupt_returns_the_focuser_to_the_start():
    dev = FakeFocuser(best=1180, start=1000)

    def interrupt(frame):
        if len(dev.moves) > 3:
            raise KeyboardInterrupt
        return {"fwhm_px": 6.0, "n_stars": 10}

    with pytest.raises(KeyboardInterrupt):
        af.run_autofocus(dev, interrupt, half_width=150, sleep=lambda s: None)
    assert dev.position == 1000


def test_it_refuses_a_focuser_that_is_not_absolute():
    dev = FakeFocuser(best=1180, start=1000)
    dev.info = lambda: {"position": 1000, "maxstep": 2600, "absolute": False}
    with pytest.raises(TelescopeError, match="absolute"):
        run(dev)


# --------------------------------------------------------------------------- #
# The Alpaca focuser
# --------------------------------------------------------------------------- #
class FakeAlpaca:
    """Just enough of the Alpaca REST client for the focuser: position, range, moves, and a camera."""

    def __init__(self, position=1000, maxstep=2600, maxincrement=2600, stall=False, overshoot=0):
        self.position, self.props = position, {"maxstep": maxstep, "maxincrement": maxincrement, "absolute": True,
                                              "stepsize": 1.0, "temperature": 7.5, "tempcomp": False, "ismoving": False}
        self.stall, self.overshoot, self.puts, self.connected = stall, overshoot, [], False

    def get(self, device, number, name, **_):
        if device == "focuser":
            return self.position if name == "position" else self.props[name]
        if device == "camera" and name == "imageready":
            return True
        raise AssertionError(name)

    def put(self, device, number, name, **fields):
        self.puts.append((device, name, fields))
        if name == "connected":
            self.connected = True
        if name == "move" and not self.stall:
            self.position = fields["Position"] + self.overshoot

    def image(self, number=0):
        return np.full((4, 4), self.position, dtype=np.uint16)


def test_the_focuser_number_is_the_telephoto_one():
    assert af.find_focuser_number(CONFIGURED) == 0
    assert af.find_focuser_number(CONFIGURED, preferred=1) == 1
    with pytest.raises(TelescopeError, match="no focuser"):
        af.find_focuser_number([{"DeviceType": "Camera", "DeviceNumber": 0}])


def test_the_alpaca_focuser_reads_its_range_and_moves_within_it():
    client = FakeAlpaca()
    dev = af.AlpacaFocuser(client, 0, sleep=lambda s: None)
    info = dev.info()
    assert client.connected and info["position"] == 1000 and info["maxstep"] == 2600
    assert dev.move_to(1200) == 1200 and ("focuser", "move", {"Position": 1200}) in client.puts


def test_the_alpaca_focuser_refuses_a_position_outside_its_range():
    dev = af.AlpacaFocuser(FakeAlpaca(), 0, sleep=lambda s: None)
    dev.info()
    for bad in (-1, 2601):
        with pytest.raises(TelescopeError, match="refusing"):
            dev.move_to(bad)


def test_a_long_move_is_split_at_the_focusers_max_increment():
    client = FakeAlpaca(position=100, maxincrement=300)
    dev = af.AlpacaFocuser(client, 0, sleep=lambda s: None)
    dev.info()
    assert dev.move_to(1000) == 1000
    steps = [f["Position"] for _d, n, f in client.puts if n == "move"]
    assert steps == [400, 700, 1000]


def test_a_focuser_that_does_not_move_is_an_error_not_a_silent_success():
    dev = af.AlpacaFocuser(FakeAlpaca(stall=True), 0, sleep=lambda s: None)
    dev.info()
    with pytest.raises(TelescopeError, match="did not move"):
        dev.move_to(1200)


def test_a_focuser_that_will_not_report_a_range_is_refused():
    client = FakeAlpaca()
    client.props["maxstep"] = None
    with pytest.raises(TelescopeError, match="range"):
        af.AlpacaFocuser(client, 0, sleep=lambda s: None).info()


def test_the_alpaca_frame_comes_from_the_telephoto_camera():
    client = FakeAlpaca()
    frame = af.AlpacaFocuser(client, 0, sleep=lambda s: None).frame(2.0, 80)
    assert frame.dtype == np.uint16 and any(n == "startexposure" for _d, n, _f in client.puts)


# --------------------------------------------------------------------------- #
# A simulated run, through the real star-width measurement
# --------------------------------------------------------------------------- #
@pytest.mark.slow
def test_a_simulated_run_focuses_on_rendered_frames():
    from core.setup_checks import measure_star_width

    target = {"name": "Focus field", "ra_deg": 150.0, "dec_deg": 11.0, "sim": {"seed": 3}}
    dev = af.MockFocuser(target, best=1180, start=1000)
    report = af.run_autofocus(dev, measure_star_width, exposure_s=3.0, half_width=150, n_points=7, sleep=lambda s: None)
    assert report["status"] == af.IMPROVED, report
    assert abs(dev.position - 1180) <= 40
    assert report["fwhm_after_px"] < report["fwhm_before_px"]


# --------------------------------------------------------------------------- #
# In the mission graph
# --------------------------------------------------------------------------- #
def _focus_mission(settings, mission_id="m_focus", tweak=None, **overrides):
    from dataclasses import replace

    from core.graph import run_mission
    from core.state import initial_state
    from plugins.asteroid_finder.plugin import AsteroidFinderPlugin
    from tests.conftest import Harness

    settings = replace(settings, max_targets=2, focus_exposure_s=3.0, autofocus_in_simulation=True, **overrides)
    plugin = AsteroidFinderPlugin(settings)
    h = Harness(settings, plugin)
    if tweak:
        tweak(h)
    h.db.create_mission(mission_id, plugin.name, True, {"cloud_cover_threshold_pct": 30.0})
    try:
        return h, run_mission(h.app, mission_id, initial_state(mission_id, plugin.name, True))
    except BaseException:
        h.close()
        raise


def test_a_mission_focuses_once_and_logs_every_step(settings):
    h, final = _focus_mission(settings)
    try:
        assert final["status"] == "COMPLETE"
        assert final["autofocus"]["status"] == af.IMPROVED, final["autofocus"]
        logs = h.db.list_logs("m_focus", kinds=["autofocus"])
        text = "\n".join(row["message"] for row in logs)
        assert text.count("starting before the first exposure") == 1, "once per mission, not once per target"
        assert "position" in text and "FWHM" in text and "improved" in text
        assert (settings.missions_dir / "m_focus" / "autofocus.json").exists()
    finally:
        h.close()


def test_the_mission_does_not_focus_in_simulation_unless_asked(settings):
    from dataclasses import replace

    assert not settings.autofocus_active
    assert replace(settings, autofocus_in_simulation=True).autofocus_active
    assert replace(settings, mock_mode=False).autofocus_active
    assert not replace(settings, mock_mode=False, autofocus=False).autofocus_active
    assert not replace(settings, mock_mode=False, data_source="archive").autofocus_active


def test_a_focus_failure_is_logged_and_the_mission_carries_on(settings):
    def broken(h):
        def focuser(target=None):
            raise TelescopeError("the focuser is not listed")
        h.telescope.focuser = focuser

    h, final = _focus_mission(settings, "m_focus_fail", tweak=broken)
    try:
        assert final["status"] == "COMPLETE"
        assert final["autofocus"]["status"] == "error"
        logs = h.db.list_logs("m_focus_fail", kinds=["autofocus"])
        assert any("skipped after an error" in row["message"] and row["level"] == "WARNING" for row in logs)
    finally:
        h.close()


def test_the_capture_hook_runs_once_the_scope_is_on_target_and_before_any_exposure(settings):
    from dataclasses import replace

    from tools.telescope import SeestarController

    order = []
    ctl = SeestarController(replace(settings), sleep_fn=lambda s: None)
    ctl.slew_to = lambda *a: order.append("slew")
    ctl.configure = lambda plan: order.append("configure")
    ctl._capture_mock = lambda *a, **kw: order.append("capture") or []
    ctl.mock, ctl.connected = True, True
    ctl.capture_sequence({"name": "x", "ra_deg": 1.0, "dec_deg": 2.0}, {"count": 1}, settings.data_dir,
                         before_exposures=lambda: order.append("hook"))
    assert order == ["slew", "configure", "hook", "capture"]


def test_the_focus_step_command_runs_the_missions_step_alone(settings, capsys, caplog):
    from dataclasses import replace

    import main

    with caplog.at_level("INFO"):
        code = main.run_focus_step_cli(replace(settings, focus_exposure_s=3.0), None)
    out = capsys.readouterr().out
    assert code == main.EXIT_OK and "Focus step: improved" in out
    assert "Autofocus: starting before the first exposure" in caplog.text and "Focus: position" in caplog.text
    assert (settings.missions_dir / "focus_step" / "autofocus.json").exists()


def test_reanalyze_says_so_when_the_mission_does_not_exist(settings, capsys):
    import main

    assert main.run_reanalyze_cli(settings, "m_nope") == main.EXIT_FAILED
    assert "there is no mission m_nope" in capsys.readouterr().err
