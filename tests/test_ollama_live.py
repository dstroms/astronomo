"""Opt-in integration test against a REAL local Ollama server.

Everything else in the suite uses a fake server that implements Ollama's *documented* API.  This
file is how you confirm the same thing against the real thing, before routing to a paid API.
It is skipped automatically (with the reason) unless Ollama is running and the model is pulled.

    ollama serve                      # if not already running
    ollama pull llama3.1              # or any model, e.g. qwen2.5-coder
    pytest -m ollama -v -rs           # run only these

    ASTRONOMO_TEST_OLLAMA_MODEL=qwen2.5-coder pytest -m ollama -v     # pick a model
    ASTRONOMO_TEST_OLLAMA_URL=http://gpu-box:11434 pytest -m ollama   # remote Ollama

The same check from the CLI, without pytest::

    python main.py --simulate --fast --ollama llama3.1
"""

import os
import re
from dataclasses import replace
from pathlib import Path

import pytest
import requests

from core.digest import looks_like_data_dump
from core.llm import LLMClient
from tests.conftest import Harness
from tests.test_graph import MID, node_sequence, run

URL = os.environ.get("ASTRONOMO_TEST_OLLAMA_URL", "http://localhost:11434")
MODEL = os.environ.get("ASTRONOMO_TEST_OLLAMA_MODEL", "llama3.1")


def _skip_reason() -> str | None:
    try:
        names = [m["name"] for m in requests.get(f"{URL}/api/tags", timeout=2).json().get("models", [])]
    except (requests.RequestException, ValueError):
        return f"no Ollama server reachable at {URL} (start it with `ollama serve`)"
    wanted = MODEL if ":" in MODEL else f"{MODEL}:latest"
    if MODEL not in names and wanted not in names:
        return f"model '{MODEL}' not pulled (run `ollama pull {MODEL}`); installed: {', '.join(names) or 'none'}"
    return None


_REASON = _skip_reason()
pytestmark = [pytest.mark.ollama, pytest.mark.skipif(_REASON is not None, reason=_REASON or "")]


@pytest.fixture()
def live_settings(settings):
    return replace(
        settings, llm_provider="ollama", llm_model_name=MODEL, llm_base_url=URL,
        llm_strict=True, llm_cache=False,  # verification: any model problem must fail loudly; really call it
        llm_timeout_s=600.0, max_targets=2,
    )


def test_preflight_check_against_the_real_server(live_settings):
    info = LLMClient(live_settings).check()
    assert info["reply"], "model returned nothing for a trivial prompt"
    assert info["prompt_tokens"] > 0 and info["completion_tokens"] > 0


def test_mock_mission_with_a_local_model_has_clean_state_transitions(settings, live_settings):
    # Baseline: the same mission with no model, to compare node paths.
    baseline_settings = replace(settings, max_targets=2, data_dir=settings.data_dir / "baseline")  # own DB + checkpoints
    baseline_settings.ensure_dirs()
    plain = Harness(baseline_settings)
    try:
        run(plain)
        baseline = node_sequence(plain)
    finally:
        plain.close()

    h = Harness(live_settings)
    try:
        final = run(h)
        assert final["status"] == "COMPLETE", final.get("errors")
        assert len(final["observations"]) == 2
        assert node_sequence(h) == baseline, "attaching a model changed the state-machine path"

        calls = h.db.list_llm_calls(MID)
        assert [(c["node"], c["ok"]) for c in calls] == [("generate_hypothesis", True)]
        for c in calls:
            assert c["response"].strip() and "<think>" not in c["response"]
            assert c["prompt_tokens"] > 0 and c["completion_tokens"] > 0
            assert len(c["prompt"]) < 3000 and looks_like_data_dump(c["prompt"]) is None
            for banned in ("reference_stars", "frame_paths", ".fits"):
                assert banned not in c["prompt"]

        text = Path(final["mission_log"]["path"]).read_text()
        assert f"ollama: {MODEL}" in text and "no text in this log" in text
        usage = h.db.llm_usage(MID)
        print(f"\n[ollama:{MODEL}] {usage}")
        print("rationale:", final["hypothesis_rationale"])
        assert re.search(r"\w", final["hypothesis_rationale"])
    finally:
        h.close()
