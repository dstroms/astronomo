"""LLMClient: provider wire formats, switching, and every token-waste control."""

import numpy as np
import pytest

from config import get_settings
from core.digest import LLMPayloadError
from core.llm import (
    LLMBudgetExceeded,
    LLMClient,
    LLMConfigError,
    LLMError,
    LLMUnavailable,
    _OpenAIAdapter,
)
from db.database import Database
from tests.llm_server import FakeLLMServer, free_port

SYSTEM = "You write short summaries."
DATA = {"id": "T01", "snr": 28.7, "quality": "good"}


@pytest.fixture()
def server():
    with FakeLLMServer() as s:
        yield s


def make(server, tmp_path, provider="ollama", **overrides):
    base = {"ollama": server.url, "openai": server.url + "/v1", "anthropic": server.url}[provider]
    model = {"ollama": "llama3.1", "openai": "gpt-test", "anthropic": "claude-test"}[provider]
    sleeps: list[float] = []
    settings = get_settings(
        data_dir=tmp_path / "data", llm_provider=provider, llm_model_name=model, llm_base_url=base,
        llm_api_key="sk-test", llm_temperature=0.0, **overrides,
    )
    return LLMClient(settings, sleep_fn=sleeps.append), sleeps


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def test_default_is_disabled_and_refuses_to_call():
    client = LLMClient(get_settings())
    assert not client.enabled and client.describe() == "disabled"
    with pytest.raises(LLMError, match="disabled"):
        client.ask("hi", None, system=SYSTEM)


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"llm_provider": "gemini"}, "unknown LLM_PROVIDER"),
        ({"llm_provider": "openai", "llm_api_key": None, "llm_model_name": "m"}, "OPENAI_API_KEY"),
        ({"llm_provider": "anthropic", "llm_api_key": None, "llm_model_name": "m"}, "ANTHROPIC_API_KEY"),
        ({"llm_provider": "anthropic", "llm_api_key": "k"}, "needs LLM_MODEL_NAME"),
        ({"llm_provider": "ollama", "llm_base_url": "localhost:11434"}, "http://host:port"),
        ({"llm_provider": "ollama", "llm_base_url": "ftp://x"}, "http://host:port"),
        ({"llm_provider": "ollama", "llm_max_input_chars": 100}, "too small"),
    ],
)
def test_bad_configuration_fails_early_with_actionable_message(monkeypatch, overrides, match):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LLMConfigError, match=match):
        LLMClient(get_settings(**overrides))


def test_ollama_needs_no_key_and_defaults_to_llama31_on_localhost(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = LLMClient(get_settings(llm_provider="ollama", llm_model_name="", llm_base_url="", llm_api_key=None))
    assert client.describe() == "ollama:llama3.1 @ http://localhost:11434"


def test_provider_key_falls_back_to_the_standard_environment_variable(monkeypatch, server, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    settings = get_settings(data_dir=tmp_path / "d", llm_provider="anthropic", llm_model_name="claude-test",
                            llm_base_url=server.url, llm_api_key=None)
    assert LLMClient(settings).ask("hi", None, system=SYSTEM).text  # server demands x-api-key == sk-test


def test_api_key_is_never_in_repr_or_public_config():
    settings = get_settings(llm_provider="openai", llm_model_name="m", llm_api_key="sk-super-secret")
    assert "sk-super-secret" not in repr(settings) and "sk-super-secret" not in str(settings.public_dict())


# --------------------------------------------------------------------------- #
# Wire formats
# --------------------------------------------------------------------------- #
def test_ollama_request_shape(server, tmp_path):
    client, _ = make(server, tmp_path, llm_num_ctx=4096, llm_max_output_tokens=200)
    resp = client.ask("Summarise.", DATA, system=SYSTEM, node="n")
    req = server.chat_requests()[0]
    assert req["path"] == "/api/chat"
    body = req["body"]
    assert body["model"] == "llama3.1" and body["stream"] is False
    assert body["options"] == {"num_predict": 200, "num_ctx": 4096, "temperature": 0.0}
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert body["messages"][0]["content"] == SYSTEM
    assert 'DATA (JSON):\n{"id":"T01","snr":28.7,"quality":"good"}' in body["messages"][1]["content"]
    assert resp.text == "The candidate looks reliable." and resp.prompt_tokens > 0 and not resp.estimated


def test_openai_compatible_request_shape_and_auth(server, tmp_path):
    client, _ = make(server, tmp_path, "openai")
    client.ask("Summarise.", DATA, system=SYSTEM)
    req = server.chat_requests()[0]
    assert req["path"] == "/v1/chat/completions" and req["headers"]["authorization"] == "Bearer sk-test"
    assert req["body"]["max_tokens"] == 400 and "max_completion_tokens" not in req["body"]  # compat servers


def test_official_openai_endpoint_uses_max_completion_tokens():
    adapter = _OpenAIAdapter("gpt-x", "https://api.openai.com/v1", "k", get_settings())
    url, headers, body = adapter.request("s", "p", 100, 0.0)
    assert url == "https://api.openai.com/v1/chat/completions" and headers["Authorization"] == "Bearer k"
    assert body["max_completion_tokens"] == 100 and "max_tokens" not in body


def test_anthropic_request_shape(server, tmp_path):
    client, _ = make(server, tmp_path, "anthropic")
    client.ask("Summarise.", DATA, system=SYSTEM)
    req = server.chat_requests()[0]
    assert req["path"] == "/v1/messages"
    assert req["headers"]["x-api-key"] == "sk-test" and req["headers"]["anthropic-version"] == "2023-06-01"
    assert req["body"]["system"] == SYSTEM and req["body"]["max_tokens"] == 400
    assert req["body"]["messages"] == [{"role": "user", "content": req["prompt"]}]


@pytest.mark.parametrize("provider", ["ollama", "openai", "anthropic"])
def test_switching_provider_changes_config_not_calling_code(server, tmp_path, provider):
    """The identical call works against all three back ends and returns the same shape."""
    client, _ = make(server, tmp_path, provider)
    resp = client.ask("Summarise.", DATA, system=SYSTEM, mission_id=None, node="n")
    assert (resp.text, resp.provider) == ("The candidate looks reliable.", provider)
    assert resp.prompt_tokens > 0 and resp.completion_tokens > 0 and not resp.cached


# --------------------------------------------------------------------------- #
# Reply cleanup
# --------------------------------------------------------------------------- #
def test_reasoning_blocks_are_stripped(server, tmp_path):
    server._reply = lambda _p: "<think>let me reason about this\nat length</think>\nFinal answer."
    client, _ = make(server, tmp_path)
    assert client.ask("q", None, system=SYSTEM).text == "Final answer."


def test_reply_that_is_only_unfinished_reasoning_is_an_error_not_empty_text(server, tmp_path):
    server._reply = lambda _p: "<think>I should consider many things and never finish"
    client, _ = make(server, tmp_path)
    with pytest.raises(LLMError, match="no usable text"):
        client.ask("q", None, system=SYSTEM)


def test_reply_cut_by_the_token_cap_is_trimmed_to_a_full_sentence(server, tmp_path):
    server._reply = lambda _p: "First sentence. Second sentence. Third sen"
    server.finish_reason_length = True
    client, _ = make(server, tmp_path)
    resp = client.ask("q", None, system=SYSTEM)
    assert resp.text == "First sentence. Second sentence." and resp.truncated


# --------------------------------------------------------------------------- #
# Token-waste controls
# --------------------------------------------------------------------------- #
def test_data_firewall_blocks_before_any_network_call(server, tmp_path):
    client, _ = make(server, tmp_path)
    for bulk in ({"pixels": np.zeros((100, 100))}, {"stars": np.random.rand(150, 2).tolist()}):
        with pytest.raises(LLMPayloadError):
            client.ask("Summarise.", bulk, system=SYSTEM)
    assert server.requests == []  # nothing was sent


def test_prompt_over_budget_is_rejected_not_truncated(server, tmp_path):
    client, _ = make(server, tmp_path, llm_max_input_chars=800)
    with pytest.raises(LLMPayloadError, match="chars"):
        client.ask("Summarise.", {"text": "word " * 200}, system=SYSTEM)
    assert server.requests == []


def test_output_cap_is_a_hard_ceiling_callers_can_only_lower(server, tmp_path):
    client, _ = make(server, tmp_path, llm_max_output_tokens=100)
    client.ask("a", None, system=SYSTEM, max_tokens=5000)
    client.ask("b", None, system=SYSTEM, max_tokens=30)
    assert [r["body"]["options"]["num_predict"] for r in server.chat_requests()] == [100, 30]


def test_identical_requests_are_served_from_cache_and_cost_zero_tokens(server, tmp_path):
    client, _ = make(server, tmp_path)
    first = client.ask("Summarise.", DATA, system=SYSTEM)
    again = client.ask("Summarise.", DATA, system=SYSTEM)
    assert len(server.chat_requests()) == 1
    assert again.cached and again.text == first.text and again.total_tokens == 0
    client.ask("Summarise.", {**DATA, "snr": 30.0}, system=SYSTEM)  # different data => real call
    assert len(server.chat_requests()) == 2


def test_cache_can_be_disabled_for_verification_runs(server, tmp_path):
    client, _ = make(server, tmp_path, llm_cache=False)
    client.ask("Summarise.", DATA, system=SYSTEM)
    client.ask("Summarise.", DATA, system=SYSTEM)
    assert len(server.chat_requests()) == 2


def test_cache_is_per_model_and_provider(server, tmp_path):
    a, _ = make(server, tmp_path, "ollama")
    b, _ = make(server, tmp_path, "openai")
    a.ask("q", DATA, system=SYSTEM)
    assert not b.ask("q", DATA, system=SYSTEM).cached


def test_per_mission_call_budget_stops_before_sending(server, tmp_path):
    settings = get_settings(data_dir=tmp_path / "d", llm_provider="ollama", llm_model_name="llama3.1",
                            llm_base_url=server.url, llm_max_calls_per_mission=2)
    db = Database(settings.db_path)
    db.create_mission("m1", "p", True)
    client = LLMClient(settings, db)
    client.ask("one", None, system=SYSTEM, mission_id="m1", node="a")
    client.ask("two", None, system=SYSTEM, mission_id="m1", node="b")
    with pytest.raises(LLMBudgetExceeded, match="2 LLM call"):
        client.ask("three", None, system=SYSTEM, mission_id="m1", node="c")
    assert len(server.chat_requests()) == 2
    usage = db.llm_usage("m1")
    assert usage["calls"] == 2 and usage["failed_calls"] == 1  # the refusal is on the ledger too
    db.dispose()


def test_token_budget_accounts_for_the_worst_case_of_the_next_call(server, tmp_path):
    settings = get_settings(data_dir=tmp_path / "d", llm_provider="ollama", llm_model_name="llama3.1",
                            llm_base_url=server.url, llm_max_tokens_per_mission=300, llm_max_output_tokens=400)
    db = Database(settings.db_path)
    db.create_mission("m1", "p", True)
    client = LLMClient(settings, db)
    with pytest.raises(LLMBudgetExceeded, match="token budget"):  # 400 output tokens alone exceed 300
        client.ask("q", None, system=SYSTEM, mission_id="m1", node="a")
    assert server.requests == []
    db.dispose()


def test_budget_survives_a_restart_because_it_lives_in_the_db(server, tmp_path):
    settings = get_settings(data_dir=tmp_path / "d", llm_provider="ollama", llm_model_name="llama3.1",
                            llm_base_url=server.url, llm_max_calls_per_mission=1, llm_cache=False)
    db = Database(settings.db_path)
    db.create_mission("m1", "p", True)
    LLMClient(settings, db).ask("one", None, system=SYSTEM, mission_id="m1", node="a")
    with pytest.raises(LLMBudgetExceeded):
        LLMClient(settings, db).ask("two", None, system=SYSTEM, mission_id="m1", node="b")  # fresh client
    db.dispose()


def test_ledger_records_the_exact_prompt_and_response(server, tmp_path):
    settings = get_settings(data_dir=tmp_path / "d", llm_provider="ollama", llm_model_name="llama3.1", llm_base_url=server.url)
    db = Database(settings.db_path)
    db.create_mission("m1", "p", True)
    client = LLMClient(settings, db)
    client.ask("Summarise.", DATA, system=SYSTEM, mission_id="m1", node="generate_hypothesis")
    client.ask("Summarise.", DATA, system=SYSTEM, mission_id="m1", node="generate_hypothesis")  # cache hit
    rows = db.list_llm_calls("m1")
    assert [r["cached"] for r in rows] == [False, True]
    assert rows[0]["node"] == "generate_hypothesis" and rows[0]["prompt"] == server.prompts()[0]
    assert rows[0]["response"] == "The candidate looks reliable." and rows[0]["prompt_tokens"] > 0
    assert rows[1]["prompt_tokens"] == 0
    assert db.llm_usage("m1")["cached_calls"] == 1
    db.dispose()


# --------------------------------------------------------------------------- #
# Failure handling
# --------------------------------------------------------------------------- #
def test_transient_failures_are_retried_with_backoff_then_succeed(server, tmp_path):
    server.failures = [(503, {"error": "overloaded"}), (429, {"error": "slow down"})]
    client, sleeps = make(server, tmp_path)
    assert client.ask("q", None, system=SYSTEM).text
    assert sleeps == [1.0, 3.0] and len(server.chat_requests()) == 3


def test_persistent_failure_gives_up_after_bounded_retries(server, tmp_path):
    server.failures = [(500, {"error": "boom"})] * 10
    client, sleeps = make(server, tmp_path)
    with pytest.raises(LLMUnavailable, match="HTTP 500"):
        client.ask("q", None, system=SYSTEM)
    assert len(server.chat_requests()) == 3 and sleeps == [1.0, 3.0]


def test_client_errors_are_not_retried(server, tmp_path):
    sleeps: list[float] = []
    bad = LLMClient(get_settings(data_dir=tmp_path / "d2", llm_provider="openai", llm_model_name="m",
                                 llm_base_url=server.url + "/v1", llm_api_key="sk-WRONG-KEY"), sleep_fn=sleeps.append)
    with pytest.raises(LLMError, match="authentication failed") as exc:
        bad.ask("q", None, system=SYSTEM)
    assert "sk-WRONG-KEY" not in str(exc.value)  # secrets never reach error text
    assert len(server.chat_requests()) == 1 and sleeps == []


def test_unknown_ollama_model_gives_a_pull_hint(server, tmp_path):
    settings = get_settings(data_dir=tmp_path / "d", llm_provider="ollama", llm_model_name="nope:1b", llm_base_url=server.url)
    with pytest.raises(LLMError, match="ollama pull nope:1b"):
        LLMClient(settings).ask("q", None, system=SYSTEM)


def test_model_that_rejects_temperature_is_retried_once_without_it(server, tmp_path):
    server.failures = [(400, {"error": {"message": "Unsupported parameter: 'temperature' is not supported with this model."}})]
    client, _ = make(server, tmp_path, "openai")
    client.ask("a", None, system=SYSTEM)
    client.ask("b", None, system=SYSTEM)
    bodies = [r["body"] for r in server.chat_requests()]
    assert "temperature" in bodies[0] and "temperature" not in bodies[1] and "temperature" not in bodies[2]


def test_unreachable_ollama_says_how_to_start_it(tmp_path):
    settings = get_settings(data_dir=tmp_path / "d", llm_provider="ollama", llm_model_name="llama3.1",
                            llm_base_url=f"http://127.0.0.1:{free_port()}", llm_timeout_s=2)
    client = LLMClient(settings, sleep_fn=lambda s: None)
    with pytest.raises(LLMUnavailable, match="ollama serve"):
        client.ask("q", None, system=SYSTEM)


def test_silent_context_truncation_by_ollama_is_detected(server, tmp_path):
    server.ollama_prompt_tokens = 4096  # the server reports a prompt that filled num_ctx
    client, _ = make(server, tmp_path, llm_num_ctx=4096)
    with pytest.raises(LLMError, match="ASTRONOMO_LLM_NUM_CTX"):
        client.ask("q", None, system=SYSTEM)


def test_missing_token_counts_are_estimated_and_flagged(server, tmp_path):
    server.ollama_omit_counts = True
    client, _ = make(server, tmp_path)
    resp = client.ask("q", DATA, system=SYSTEM)
    assert resp.estimated and resp.prompt_tokens > 0 and resp.completion_tokens > 0


# --------------------------------------------------------------------------- #
# Preflight check
# --------------------------------------------------------------------------- #
def test_check_succeeds_and_reports_installed_models(server, tmp_path):
    client, _ = make(server, tmp_path)
    info = client.check()
    assert info["reply"] and "llama3.1:latest" in info["installed_models"]
    assert server.chat_requests()[0]["body"]["options"]["num_predict"] == 64  # tiny ping


def test_check_explains_a_missing_model_before_spending_anything(server, tmp_path):
    settings = get_settings(data_dir=tmp_path / "d", llm_provider="ollama", llm_model_name="mistral", llm_base_url=server.url)
    with pytest.raises(LLMError, match="ollama pull mistral"):
        LLMClient(settings).check()
    assert server.chat_requests() == []


def test_check_with_a_pinned_tag_matches_exactly(server, tmp_path):
    settings = get_settings(data_dir=tmp_path / "d", llm_provider="ollama", llm_model_name="qwen2.5-coder:7b", llm_base_url=server.url)
    assert LLMClient(settings).check()["model"] == "qwen2.5-coder:7b"
