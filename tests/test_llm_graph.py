"""Full missions with a language model attached.

The headline property under test: whichever provider is configured, the graph's state transitions
are identical, the model only ever receives concise digests, and LLM trouble never costs the
science.  The model writes one thing, the observing rationale (``generate_hypothesis``); the mission
log and upload files never contain model text.  The data firewall itself (``core/digest.py``) is
tested in ``test_digest.py``.
"""

import re
from dataclasses import replace
from pathlib import Path

import pytest

from core.digest import looks_like_data_dump
from tests.conftest import Harness, make_settings, offline_world
from tests.llm_server import FakeLLMServer
from tests.test_graph import MID, node_sequence, run

FORBIDDEN_IN_PROMPTS = ("reference_stars", "frame_paths", "points", "wcs", "crval", ".fits", ".fit\"", "/data/", "registration")


@pytest.fixture()
def server():
    with FakeLLMServer() as s:
        yield s


def llm_settings(settings, server, provider="ollama", **overrides):
    base = {"ollama": server.url, "openai": server.url + "/v1", "anthropic": server.url}[provider]
    return replace(settings, llm_provider=provider, llm_model_name="llama3.1" if provider == "ollama" else "test-model",
                   llm_base_url=base, llm_api_key="sk-test", llm_cache=False, **overrides)


def number_count(text: str) -> int:
    return len(re.findall(r"[-+]?\d+\.?\d*", text))


def log_of(final) -> str:
    return Path(final["mission_log"]["path"]).read_text()


# --------------------------------------------------------------------------- #
# Provider-agnostic state transitions
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def llm_mission(tmp_path_factory):
    """One mission with the default (ollama) provider and one target, read by several tests below."""
    with offline_world(), FakeLLMServer() as server:
        h = Harness(llm_settings(make_settings(tmp_path_factory.mktemp("llm") / "data"), server, max_targets=1))
        try:
            final = run(h)
        except BaseException:
            h.close()
            raise
        try:
            yield server, h, final
        finally:
            h.close()


def _assert_completes_like_a_mission_without_a_model(provider, h, final):
    assert final["status"] == "COMPLETE" and len(final["observations"]) == 1
    # Same node path as a mission without any model:
    seq = node_sequence(h)
    assert seq[:2] == ["generate_hypothesis", "check_weather"]
    assert seq.count("execute_observation") == 1 and seq[-1] == "write_log"
    # Exactly one model call for the whole mission, regardless of target count.
    calls = h.db.list_llm_calls(MID)
    assert [(c["node"], c["ok"]) for c in calls] == [("generate_hypothesis", True)]
    assert final["hypothesis_rationale"] == "The candidate looks reliable."
    text = log_of(final)
    assert f"{provider}: " in text and "no text in this log" in text  # provenance in the reproducibility table
    assert "The candidate looks reliable." not in text  # the log never carries model-written text


def test_mission_completes_identically_on_the_default_provider(llm_mission):
    _, h, final = llm_mission
    _assert_completes_like_a_mission_without_a_model("ollama", h, final)


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_mission_completes_identically_on_every_provider(settings, server, provider):
    h = Harness(llm_settings(settings, server, provider, max_targets=1))
    try:
        _assert_completes_like_a_mission_without_a_model(provider, h, run(h))
    finally:
        h.close()


def test_no_model_configured_means_zero_calls(settings, server):
    harness = Harness(replace(settings, max_targets=1))
    try:
        final = run(harness)
    finally:
        harness.close()
    assert harness.db.list_llm_calls(MID) == [] and server.requests == []
    assert "hypothesis_rationale" not in final
    assert "none (all text is deterministic)" in log_of(final)


# --------------------------------------------------------------------------- #
# The data-reduction rule, checked on the wire
# --------------------------------------------------------------------------- #
def test_model_only_ever_sees_concise_digests_never_raw_data(llm_mission):
    server, _, final = llm_mission
    prompts = server.prompts()
    assert len(prompts) == 1
    for prompt in prompts:
        assert len(prompt) < 3000  # a few hundred tokens, not thousands
        assert looks_like_data_dump(prompt) is None
        assert number_count(prompt) < 120
        for banned in FORBIDDEN_IN_PROMPTS:
            assert banned not in prompt, f"'{banned}' leaked into a prompt"
    for req in server.chat_requests():
        assert req["system"] and "NOT available to you" in req["system"]  # the standing rules are sent


# --------------------------------------------------------------------------- #
# LLM trouble never costs the science (unless strict)
# --------------------------------------------------------------------------- #
def test_llm_outage_falls_back_to_deterministic_text(settings, server):
    server.failures = [(500, {"error": "down"})] * 20
    h = Harness(llm_settings(settings, server, max_targets=2))
    try:
        final = run(h)
        assert final["status"] == "COMPLETE" and len(final["observations"]) == 2
        assert "hypothesis_rationale" not in final
        assert any(e.startswith("llm(generate_hypothesis)") for e in final["errors"])
        assert h.db.get_mission(MID)["error"] is None  # an LLM hiccup is not a mission error
        assert h.llm_sleeps  # it did retry (with injected, instant sleeps)
        assert h.db.llm_usage(MID)["failed_calls"] == 1
    finally:
        h.close()


def test_strict_mode_fails_the_mission_loudly(settings, server):
    server.failures = [(500, {"error": "down"})] * 20
    h = Harness(llm_settings(settings, server, llm_strict=True))
    try:
        final = run(h)
        assert final["status"] == "FAILED"
        assert "generate_hypothesis" in final["errors"][-1] and "HTTP 500" in final["errors"][-1]
        assert h.captures == 0  # failed before touching the telescope
    finally:
        h.close()


def test_per_mission_call_budget_degrades_gracefully(settings, server):
    h = Harness(llm_settings(settings, server, max_targets=1, llm_max_calls_per_mission=0))
    try:
        final = run(h)
        assert final["status"] == "COMPLETE"
        assert len(server.chat_requests()) == 0  # the call was refused before sending
        assert any("generate_hypothesis" in e and "LLM call" in e for e in final["errors"])
    finally:
        h.close()


def test_usage_is_visible_in_the_ledger_and_the_log_table(llm_mission):
    _, h, final = llm_mission
    usage = h.db.llm_usage(MID)
    assert usage["calls"] == 1 and usage["total_tokens"] > 0 and usage["max_prompt_chars"] < 3000
    assert re.search(r"Language model \| ollama: llama3\.1 \(1 call", log_of(final))
