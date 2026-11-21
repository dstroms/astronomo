"""`.env` loading and the LLM_* / ASTRONOMO_LLM_* aliases, exercised through a clean child process
(config is read at import time, so this is the only honest way to test the real path)."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import config

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")

CHILD = """
import json, config
from core.llm import LLMClient
s = config.get_settings()
print(json.dumps({"describe": LLMClient(s).describe(), "strict": s.llm_strict, "max_calls": s.llm_max_calls_per_mission}))
"""


def child_env(extra: dict) -> dict:
    scrub = re.compile(r"^(ASTRONOMO_|LLM_|OPENAI_API_KEY|ANTHROPIC_API_KEY)")
    env = {k: v for k, v in os.environ.items() if not scrub.match(k)}  # incl. the suite's ASTRONOMO_NO_DOTENV
    env["PYTHONPATH"] = str(ROOT)
    return {**env, **extra}


def run_child(tmp_path, dotenv: str | None, **env) -> dict:
    extra = dict(env)
    if dotenv is not None:
        path = tmp_path / ".env"
        path.write_text(dotenv, encoding="utf-8")
        extra["ASTRONOMO_DOTENV"] = str(path)
    out = subprocess.run([sys.executable, "-c", CHILD], cwd=ROOT, env=child_env(extra), capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def uncomment(option: str) -> str:
    """The shipped .env.example with the block that starts at '# Option <X>' uncommented."""
    lines, out, active = EXAMPLE.splitlines(), [], False
    for line in lines:
        if line.startswith(f"# Option {option}"):
            active = True
        elif active and not line.strip():
            active = False
        elif active and line.startswith("# "):
            line = line[2:]
        out.append(line)
    text = "\n".join(out)
    return text.replace("<model-id>", "model-x").replace("<served-model-name>", "model-x").replace("<your-key>", "key-x")


# --------------------------------------------------------------------------- #
# The shipped .env.example really works as documented
# --------------------------------------------------------------------------- #
def test_env_example_copied_verbatim_means_no_model(tmp_path):
    assert run_child(tmp_path, EXAMPLE)["describe"] == "disabled"


def test_option_a_local_ollama(tmp_path):
    got = run_child(tmp_path, uncomment("A"))
    assert got["describe"] == "ollama:qwen2.5-coder:7b @ http://localhost:11434"


def test_option_b_anthropic_reads_its_key_from_the_dotenv(tmp_path):
    assert run_child(tmp_path, uncomment("B"))["describe"] == "anthropic:model-x @ https://api.anthropic.com"


def test_option_c_openai(tmp_path):
    assert run_child(tmp_path, uncomment("C"))["describe"] == "openai:model-x @ https://api.openai.com/v1"


def test_option_d_openai_compatible_local_server_needs_no_key(tmp_path):
    assert run_child(tmp_path, uncomment("D"))["describe"] == "openai:model-x @ http://localhost:8000/v1"


def test_cloud_option_without_a_key_fails_with_the_actionable_message(tmp_path):
    text = uncomment("B").replace("ANTHROPIC_API_KEY=key-x", "")
    out = subprocess.run([sys.executable, "-c", CHILD], cwd=ROOT, env=child_env({"ASTRONOMO_DOTENV": str(_write(tmp_path, text))}),
                         capture_output=True, text=True, timeout=120)
    assert out.returncode != 0 and "ANTHROPIC_API_KEY" in out.stderr


def _write(tmp_path, text) -> Path:
    p = tmp_path / ".env"
    p.write_text(text, encoding="utf-8")
    return p


def test_optional_limits_in_the_dotenv_are_honoured(tmp_path):
    got = run_child(tmp_path, "LLM_PROVIDER=ollama\nLLM_STRICT=true\nLLM_MAX_CALLS_PER_MISSION=3\n")
    assert got["strict"] is True and got["max_calls"] == 3


# --------------------------------------------------------------------------- #
# Precedence
# --------------------------------------------------------------------------- #
def test_real_environment_beats_the_dotenv(tmp_path):
    got = run_child(tmp_path, "LLM_PROVIDER=ollama\n", LLM_PROVIDER="none")
    assert got["describe"] == "disabled"


def test_prefixed_name_beats_bare_name(tmp_path):
    got = run_child(tmp_path, "LLM_PROVIDER=none\nASTRONOMO_LLM_PROVIDER=ollama\nLLM_MODEL_NAME=m\n")
    assert got["describe"].startswith("ollama:m ")


def test_no_dotenv_switch_ignores_the_file(tmp_path):
    assert run_child(tmp_path, "LLM_PROVIDER=ollama\n", ASTRONOMO_NO_DOTENV="1")["describe"] == "disabled"


# --------------------------------------------------------------------------- #
# Parser details
# --------------------------------------------------------------------------- #
def parse(tmp_path, monkeypatch, text, preset=None) -> dict:
    monkeypatch.setattr(os, "environ", dict(preset or {}))
    config._load_dotenv(_write(tmp_path, text))
    return dict(os.environ)


def test_parser_handles_comments_quotes_export_and_blank_lines(tmp_path, monkeypatch):
    env = parse(tmp_path, monkeypatch, "\n# comment\nA=1\nexport B = two  # trailing\nC=\"quoted # not a comment\"\nD='single'\nE=\nnot a pair\n9X=bad\n")
    assert env == {"A": "1", "B": "two", "C": "quoted # not a comment", "D": "single", "E": ""}


def test_parser_last_duplicate_wins_but_real_environment_still_wins(tmp_path, monkeypatch):
    env = parse(tmp_path, monkeypatch, "LLM_PROVIDER=none\nLLM_PROVIDER=ollama\nKEEP=file\n", preset={"KEEP": "real"})
    assert env == {"LLM_PROVIDER": "ollama", "KEEP": "real"}


def test_missing_file_is_fine(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", {})
    config._load_dotenv(tmp_path / "nope.env")
    assert os.environ == {}


# --------------------------------------------------------------------------- #
# Identity is checked when settings are built, and an empty code stays legal
# --------------------------------------------------------------------------- #
def test_an_empty_observer_code_and_station_code_are_allowed(tmp_path):
    s = config.get_settings(aavso_obs_code="", mpc_station_code="", data_dir=tmp_path)
    assert s.aavso_obs_code == "" and s.mpc_station_code == ""


def test_a_well_formed_identity_is_accepted(tmp_path):
    s = config.get_settings(aavso_obs_code="AB1", mpc_station_code="G58", data_dir=tmp_path)
    assert s.aavso_obs_code == "AB1" and s.mpc_station_code == "G58"


def test_a_malformed_observer_code_is_refused_when_settings_are_built(tmp_path):
    with pytest.raises(ValueError, match="ASTRONOMO_AAVSO_OBS_CODE"):
        config.get_settings(aavso_obs_code="nope!", data_dir=tmp_path)


def test_a_malformed_station_code_is_refused_when_settings_are_built(tmp_path):
    with pytest.raises(ValueError, match="ASTRONOMO_MPC_STATION_CODE"):
        config.get_settings(mpc_station_code="AB", data_dir=tmp_path)


def test_main_prints_a_bad_observer_code_and_does_not_start(tmp_path):
    env = child_env({
        "ASTRONOMO_AAVSO_OBS_CODE": "nope!",
        "ASTRONOMO_NO_DOTENV": "1",
        "ASTRONOMO_DATA_DIR": str(tmp_path),
    })
    out = subprocess.run(
        [sys.executable, "main.py", "--simulate", "--fast"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
    )
    assert out.returncode != 0
    assert "ASTRONOMO_AAVSO_OBS_CODE" in out.stderr
    assert "Mission" not in out.stdout
