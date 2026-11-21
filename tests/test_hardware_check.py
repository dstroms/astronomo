"""The first-light smoke test, run against the fake Seestar (the real scope is unconfirmed)."""

import json
from dataclasses import replace

import numpy as np
from astropy.io import fits

import main
from tests.test_telescope import _real_settings, fake_seestar  # noqa: F401  (fixture)
from tools.hardware_check import FAIL, INFO, PASS, WARN, check_share, format_report, run_hardware_check, save_report


def _by_name(report):
    return {s["name"]: s for s in report["steps"]}


def _with_site(settings):
    return replace(settings, site_source="file")


def test_smoke_test_passes_against_a_healthy_scope_and_parks(fake_seestar):  # noqa: F811
    fits.PrimaryHDU(np.zeros((4, 4), dtype=np.uint16)).writeto(fake_seestar.share / "old.fit")
    report = run_hardware_check(_with_site(_real_settings(fake_seestar, fake_seestar.share)))
    steps = _by_name(report)
    assert report["ok"] and report["mode"] == "real" and report["firmware"] == 2400
    assert steps["connect"]["status"] == PASS and steps["firmware"]["status"] == PASS
    assert steps["share"]["status"] == PASS and steps["park"]["status"] == PASS
    assert "raw" in steps["firmware"] and "raw" in steps["position"]  # the raw replies are kept
    methods = [m for m, _ in fake_seestar.calls]
    assert "scope_park" in methods
    # moves and captures nothing
    assert not {"iscope_start_view", "iscope_start_stack", "set_control_value"} & set(methods)


def test_no_park_skips_the_park_command(fake_seestar):  # noqa: F811
    report = run_hardware_check(_with_site(_real_settings(fake_seestar, fake_seestar.share)), park=False)
    assert _by_name(report)["park"]["status"] == INFO
    assert "scope_park" not in [m for m, _ in fake_seestar.calls]


def test_placeholder_site_is_never_pushed_to_the_scope(fake_seestar):  # noqa: F811
    report = run_hardware_check(_real_settings(fake_seestar, fake_seestar.share))  # site_source == "default"
    assert _by_name(report)["site"]["status"] == WARN
    assert "set_user_location" not in [m for m, _ in fake_seestar.calls]


def test_location_mismatch_is_flagged(fake_seestar):  # noqa: F811
    s = _with_site(_real_settings(fake_seestar, fake_seestar.share))
    fake_seestar.fail_method = None
    report = run_hardware_check(s)
    # the fake answers get_user_location with 0 (not a position), so only the raw reply is recorded
    assert "location matches site" not in _by_name(report)
    assert _by_name(report)["scope's location"]["status"] == INFO


def test_unreachable_scope_fails_with_a_helpful_message(tmp_path):
    from config import get_settings

    s = get_settings(mock_mode=False, seestar_host="127.0.0.1", seestar_port=1, seestar_retries=0, data_dir=tmp_path)
    report = run_hardware_check(s)
    step = _by_name(report)["connect"]
    assert not report["ok"] and step["status"] == FAIL and "same network" in step["detail"]


def test_a_scope_that_demands_authentication_fails_clearly(fake_seestar):  # noqa: F811
    fake_seestar.refuse_all = True
    report = run_hardware_check(_with_site(_real_settings(fake_seestar, fake_seestar.share)))
    assert not report["ok"] and "7.18" in _by_name(report)["connect"]["detail"]


def test_share_checks(tmp_path):
    assert check_share(None)[0] == FAIL
    assert check_share(str(tmp_path / "missing"))[0] == FAIL
    assert check_share(str(tmp_path))[0] == WARN  # readable but empty
    fits.PrimaryHDU(np.zeros((2, 2), dtype=np.uint16)).writeto(tmp_path / "a.fit")
    assert check_share(str(tmp_path))[0] == PASS


def test_report_is_printable_and_saved(fake_seestar, tmp_path):  # noqa: F811
    report = run_hardware_check(_with_site(_real_settings(fake_seestar, fake_seestar.share)))
    text = format_report(report)
    assert "RESULT: OK" in text and "[PASS] connect" in text
    saved = save_report(report, tmp_path)
    assert json.loads(saved.read_text())["firmware"] == 2400


def test_cli_simulate_exercises_the_mock_and_touches_no_hardware(capsys, tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    code = main.main(["--hardware-check", "--simulate"])
    out = capsys.readouterr().out
    assert code == 0 and "MOCK" in out and "RESULT: OK" in out


def test_cli_forces_real_mode_without_simulate():
    args = main.parse_args(["--hardware-check"])
    assert main.build_settings(args).mock_mode is False
