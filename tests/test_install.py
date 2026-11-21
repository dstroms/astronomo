"""The installer writes an identity file and leaves the telescope mock on.

The venv and the fast simulation are not built here except for one slow test. Creating
``.venv`` downloads the scientific stack, and the simulation is a whole mission.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

import install

ROOT = Path(__file__).resolve().parent.parent


def _root(tmp_path) -> Path:
    (tmp_path / ".env.example").write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
    return tmp_path


def _namespace(values: dict):
    import argparse
    return argparse.Namespace(**values)


def test_python_3_9_is_refused_and_3_10_is_not():
    assert "3.9" in install.python_too_old((3, 9, 18))
    assert install.python_too_old((3, 10, 0)) is None
    assert install.python_too_old((3, 12, 1)) is None


def test_a_missing_venv_would_be_created_and_then_installed(tmp_path):
    calls = []

    def run(cmd, cwd=None):
        calls.append((list(cmd), cwd))

    install.ensure_venv(tmp_path, run)
    assert calls[0][0][1:3] == ["-m", "venv"]
    assert calls[1][0][1:] == ["-m", "pip", "install", "-e", ".[dev]"]
    assert calls[1][1] == tmp_path


def test_a_venv_without_the_package_is_installed_again(tmp_path):
    py = tmp_path / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("", encoding="utf-8")
    calls = []
    install.ensure_venv(tmp_path, lambda cmd, cwd=None: calls.append(list(cmd)))
    assert len(calls) == 1 and calls[0][1:] == ["-m", "pip", "install", "-e", ".[dev]"]


def test_an_installed_venv_is_left_alone(tmp_path):
    bindir = tmp_path / ".venv" / "bin"
    bindir.mkdir(parents=True)
    (bindir / "python").write_text("", encoding="utf-8")
    (bindir / "astronomo").write_text("", encoding="utf-8")
    calls = []
    install.ensure_venv(tmp_path, lambda cmd, cwd=None: calls.append(cmd))
    assert calls == []


def test_noninteractive_setup_writes_the_identity_and_leaves_the_mock_on(tmp_path):
    root = _root(tmp_path)
    status = install.apply_setup(root, _namespace(dict(
        non_interactive=True, replace_env=False, no_self_test=True, no_install=True,
        root=root, data_dir=None, observer_name="Ada Lovelace", aavso_code="AB1", mpc_code="G58",
        site_name="Back garden", site_lat="51.5", site_lon="-0.12", site_elev="20",
    )), interactive=False)
    assert status == 0
    text = (root / ".env").read_text(encoding="utf-8")
    assert "ASTRONOMO_OBSERVER_NAME=" in text and "Ada Lovelace" in text
    assert "ASTRONOMO_AAVSO_OBS_CODE=AB1" in text
    assert "ASTRONOMO_MPC_STATION_CODE=G58" in text
    assert "ASTRONOMO_SITE_LAT=" in text
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        assert "MOCK_MODE" not in line
        assert "API_KEY" not in line
        assert "SELF_START" not in line
        assert "SEESTAR_TRANSPORT" not in line


def test_an_existing_env_is_kept_unless_replacement_is_asked_for(tmp_path):
    root = _root(tmp_path)
    dest = root / ".env"
    dest.write_text("KEEP=1\n", encoding="utf-8")
    kept = install.apply_setup(root, _namespace(dict(
        non_interactive=True, replace_env=False, no_self_test=True, no_install=True,
        root=root, data_dir=None, observer_name=None, aavso_code="AB1", mpc_code=None,
        site_name=None, site_lat=None, site_lon=None, site_elev=None,
    )), interactive=False)
    assert kept == 1 and dest.read_text(encoding="utf-8") == "KEEP=1\n"
    replaced = install.apply_setup(root, _namespace(dict(
        non_interactive=True, replace_env=True, no_self_test=True, no_install=True,
        root=root, data_dir=None, observer_name=None, aavso_code="AB1", mpc_code=None,
        site_name=None, site_lat=None, site_lon=None, site_elev=None,
    )), interactive=False)
    assert replaced == 0 and "ASTRONOMO_AAVSO_OBS_CODE=AB1" in dest.read_text(encoding="utf-8")


def test_a_bad_code_or_a_partial_site_is_refused_before_writing(tmp_path):
    root = _root(tmp_path)
    bad_code = install.apply_setup(root, _namespace(dict(
        non_interactive=True, replace_env=False, no_self_test=True, no_install=True,
        root=root, data_dir=None, observer_name=None, aavso_code="nope!", mpc_code=None,
        site_name=None, site_lat=None, site_lon=None, site_elev=None,
    )), interactive=False)
    assert bad_code == 1 and not (root / ".env").exists()
    partial = install.apply_setup(root, _namespace(dict(
        non_interactive=True, replace_env=False, no_self_test=True, no_install=True,
        root=root, data_dir=None, observer_name=None, aavso_code=None, mpc_code=None,
        site_name=None, site_lat="10", site_lon=None, site_elev=None,
    )), interactive=False)
    assert partial == 1 and not (root / ".env").exists()


def test_the_self_test_is_a_fast_simulation(tmp_path):
    root = _root(tmp_path)
    seen = {}

    def run(cmd, cwd=None, env=None):
        seen["cmd"] = cmd
        seen["env"] = env
        return subprocess.CompletedProcess(cmd, 0)

    status = install.apply_setup(root, _namespace(dict(
        non_interactive=True, replace_env=False, no_self_test=False, no_install=True,
        root=root, data_dir=tmp_path / "data", observer_name=None, aavso_code=None, mpc_code=None,
        site_name=None, site_lat=None, site_lon=None, site_elev=None,
    )), interactive=False, run=run)
    assert status == 0
    assert seen["cmd"][-2:] == ["--simulate", "--fast"]
    assert seen["env"]["ASTRONOMO_DOTENV"].endswith(".env")
    assert "ASTRONOMO_NO_DOTENV" not in seen["env"]


def test_the_next_step_is_the_api_ui(tmp_path, capsys):
    root = _root(tmp_path)
    status = install.apply_setup(root, _namespace(dict(
        non_interactive=True, replace_env=False, no_self_test=True, no_install=True,
        root=root, data_dir=None, observer_name=None, aavso_code=None, mpc_code=None,
        site_name=None, site_lat=None, site_lon=None, site_elev=None,
    )), interactive=False)
    assert status == 0
    out = capsys.readouterr().out
    assert "python -m dashboard.api" in out
    assert "http://localhost:8765" in out


def test_the_command_writes_a_file_the_loader_accepts(tmp_path):
    root = _root(tmp_path)
    assert install.main([
        "--non-interactive", "--no-install", "--no-self-test",
        "--root", str(root), "--observer-name", "Ada Lovelace", "--aavso-code", "AB1",
        "--site-lat", "51.5", "--site-lon", "-0.12", "--site-elev", "20",
    ]) == 0
    env = {k: v for k, v in __import__("os").environ.items() if not k.startswith(("ASTRONOMO_", "LLM_"))}
    env["PYTHONPATH"] = str(ROOT)
    env["ASTRONOMO_DOTENV"] = str(root / ".env")
    out = subprocess.run(
        [sys.executable, "-c", "import config; s = config.get_settings(); print(s.aavso_obs_code, s.observer_name, round(s.site_latitude_deg, 1))"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60, check=False,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "AB1 Ada Lovelace 51.5"


def test_requirements_and_pyproject_name_the_same_packages():
    req = [
        line.split("#", 1)[0].strip()
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.split("#", 1)[0].strip()
    ]
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    deps = re.findall(r'^    "([^"]+)",$', text, re.M)
    assert sorted(req) == sorted(deps)


@pytest.mark.slow
def test_the_fast_simulation_actually_runs(tmp_path):
    root = _root(tmp_path)
    status = install.main([
        "--non-interactive", "--no-install",
        "--root", str(root), "--data-dir", str(tmp_path / "data"),
    ])
    assert status == 0
