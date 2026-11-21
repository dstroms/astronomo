"""main.py's language-model options, exercised end to end against the fake Ollama."""

import pytest

import config
import main
from tests.llm_server import FakeLLMServer, free_port

pytestmark = pytest.mark.slow  # every test here runs a whole mission


@pytest.fixture()
def server():
    with FakeLLMServer() as s:
        yield s


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")  # Settings reads this at construction


def settings_for(*argv):
    return main.build_settings(main.parse_args(list(argv)))


def test_ollama_shortcut_means_local_strict_uncached():
    s = settings_for("--simulate", "--ollama", "qwen2.5-coder")
    assert (s.llm_provider, s.llm_model_name, s.llm_strict, s.llm_cache) == ("ollama", "qwen2.5-coder", True, False)


def test_ollama_shortcut_without_a_model_uses_the_provider_default():
    s = settings_for("--ollama")
    assert s.llm_provider == "ollama" and s.llm_model_name == config.LLM_MODEL_NAME  # "" => llama3.1 in the client


def test_explicit_flags_win_over_the_shortcut_and_cloud_flags_work():
    s = settings_for("--ollama", "llama3.1", "--llm-model", "qwen2.5-coder:7b", "--llm-base-url", "http://gpu-box:11434")
    assert (s.llm_model_name, s.llm_base_url) == ("qwen2.5-coder:7b", "http://gpu-box:11434")
    c = settings_for("--llm-provider", "anthropic", "--llm-model", "some-model", "--llm-no-cache", "--llm-strict")
    assert (c.llm_provider, c.llm_cache, c.llm_strict) == ("anthropic", False, True)


def test_default_settings_have_no_model_and_default_cache_on():
    s = settings_for("--simulate")
    assert s.llm_provider == "none" and s.llm_cache and not s.llm_strict


def test_llm_check_ok(server, capsys):
    code = main.main(["--llm-check", "--llm-provider", "ollama", "--llm-model", "llama3.1", "--llm-base-url", server.url])
    out = capsys.readouterr().out
    assert code == 0 and "LLM check OK: ollama:llama3.1" in out and "qwen2.5-coder:7b" in out


def test_llm_check_missing_model_says_how_to_fix_it(server, capsys):
    code = main.main(["--llm-check", "--llm-provider", "ollama", "--llm-model", "mistral", "--llm-base-url", server.url])
    assert code == 1 and "ollama pull mistral" in capsys.readouterr().err


def test_llm_check_with_ollama_down(capsys):
    code = main.main(["--llm-check", "--llm-provider", "ollama", "--llm-base-url", f"http://127.0.0.1:{free_port()}"])
    assert code == 1 and "ollama serve" in capsys.readouterr().err


def test_llm_check_needs_a_provider(capsys):
    assert main.main(["--llm-check"]) == 1 and "none" in capsys.readouterr().err


def test_bad_llm_config_is_reported_before_anything_runs(capsys):
    assert main.main(["--simulate", "--llm-provider", "anthropic", "--llm-model", "m"]) == 1  # no API key
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_simulated_mission_through_the_cli_with_a_local_model(server, capsys):
    code = main.main(["--simulate", "--fast", "--new", "--max-targets", "1", "--ollama", "llama3.1", "--llm-base-url", server.url])
    out = capsys.readouterr().out
    assert code == 0
    assert "llm=ollama:llama3.1" in out and "(strict)" in out
    assert "Mission" in out and "finished: COMPLETE" in out
    assert "llm: 1 call(s)" in out and "0 cache hit(s)" in out and "0 failed" in out
    assert len(server.chat_requests()) == 1


def test_strict_cli_run_fails_the_mission_when_the_model_errors(server, capsys):
    server.failures = [(400, {"error": "model rejected the request"})] * 5  # non-transient: no retry delay
    code = main.main(["--simulate", "--fast", "--new", "--max-targets", "1", "--ollama", "llama3.1", "--llm-base-url", server.url])
    out = capsys.readouterr().out
    assert code == 1 and "finished: FAILED" in out and "generate_hypothesis" in out


def test_non_strict_cli_run_survives_the_same_error(server, capsys):
    server.failures = [(400, {"error": "model rejected the request"})] * 5
    code = main.main(["--simulate", "--fast", "--new", "--max-targets", "1", "--llm-provider", "ollama",
                      "--llm-model", "llama3.1", "--llm-base-url", server.url])
    out = capsys.readouterr().out
    assert code == 0 and "finished: COMPLETE" in out and "! llm(generate_hypothesis)" in out
