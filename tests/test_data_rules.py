"""Deterministic-data-reduction rules, audited.

1. ``reduction.py`` output has a concise, model-safe summary; the heavy findings stay in-process.
2. Nothing in tools/, plugins/, writer/ or db/ can reach a language model: only ``core.graph``
   calls the client, and only with digests.
"""

import copy
import json
import re
from pathlib import Path

import pytest

from core.digest import LLMPayloadError, render_payload
from plugins.asteroid_finder.reduction import analyze, summarize
from tests.test_reduction import _capture

ROOT = Path(__file__).resolve().parent.parent
HEAVY_KEYS = ("reference_stars", "points", "wcs", "frame_paths", "registration", "n_sources_per_frame", "frame_x", "figures")


@pytest.mark.slow
def test_reduction_emits_a_concise_summary_that_passes_the_firewall(settings, tmp_path):
    mover = {"dx_px": -60, "dy_px": 20, "vx_px_s": 0.25, "vy_px_s": -0.10, "mag": 14.5}
    findings = analyze(_capture(settings, tmp_path, "sum", 7, mover), figure_dir=tmp_path / "fig")

    summary = findings["agent_summary"]
    text = render_payload(summary)  # raises if it breaks any data-reduction rule
    assert len(text) < 1000
    for key in HEAVY_KEYS:
        assert f'"{key}"' not in text
    assert summary["n_tracks"] == 1 and summary["n_frames_used"] == 4
    track = summary["tracks"][0]
    assert {"id", "ra_deg", "dec_deg", "rate_arcsec_per_min", "snr", "n_detections", "n_frames", "quality"} <= track.keys()
    assert track["quality"] == "good" and track["n_detections"] == 4

    # The full findings are what the dashboard/cross-reference need -- and exactly what must never be sent:
    heavy = {k: v for k, v in findings.items() if k != "agent_summary"}
    assert len(json.dumps(heavy, default=str)) > 20 * len(text)
    with pytest.raises(LLMPayloadError):
        render_payload(heavy)
    assert findings["astrometry"]["reference_stars"] and findings["detections"][0]["points"]  # still available in-process


@pytest.mark.slow
def test_summary_for_a_null_field_and_for_too_few_frames_is_also_safe(settings, tmp_path):
    null = analyze(_capture(settings, tmp_path, "null", 101, None))
    assert null["agent_summary"]["n_tracks"] == 0 and null["agent_summary"]["tracks"] == []
    render_payload(null["agent_summary"])

    few = analyze(_capture(settings, tmp_path, "few", 5, None)[:2])
    assert few["agent_summary"]["warnings"] and "usable frame" in few["agent_summary"]["warnings"][0]
    render_payload(few["agent_summary"])


def test_summarize_ranks_by_snr_caps_tracks_and_is_pure():
    detections = [
        {"id": f"T{i:02d}", "snr_median": float(i), "ra_deg": 150.0 + i / 7, "dec_deg": 11.0, "n_points": 4, "n_frames": 4,
         "quality": "good", "rate_arcsec_per_min": 1.23456789, "motion_px": 8.0, "rms_px": 0.3, "points": [{"x": 1.0}] * 4}
        for i in range(12)
    ]
    findings = {"n_frames": 4, "n_frames_used": 4, "detections": detections, "warnings": ["w" * 400] * 5,
                "astrometry": {"reference_stars": [[1.0, 2.0]] * 150}}
    before = copy.deepcopy(findings)
    summary = summarize(findings, max_tracks=5)
    assert findings == before  # pure
    assert [t["id"] for t in summary["tracks"]] == ["T11", "T10", "T09", "T08", "T07"]
    assert summary["n_tracks"] == 12 and summary["n_tracks_omitted"] == 7
    assert len(summary["warnings"]) == 3 and all(len(w) <= 160 for w in summary["warnings"])
    assert summary["tracks"][0]["rate_arcsec_per_min"] == 1.235  # rounded to cut tokens
    assert summary["tracks"][0]["ra_deg"] == round(150.0 + 11 / 7, 4)  # position keeps fixed decimals
    render_payload(summary)


def test_summarize_tolerates_missing_and_nan_fields():
    summary = summarize({"detections": [{"id": "T01", "snr_median": float("nan"), "ra_deg": None}]})
    assert summary["tracks"] == [{"id": "T01"}]
    render_payload(summary)


# --------------------------------------------------------------------------- #
# Static audit: who can reach a model?
# --------------------------------------------------------------------------- #
def _source_files(*dirs: str):
    for d in dirs:
        yield from (ROOT / d).rglob("*.py")


APP_DIRS = ("core", "db", "tools", "plugins", "writer", "dashboard")


@pytest.mark.parametrize("pattern", [r"core\.llm", r"\bimport\s+(openai|anthropic|ollama)\b", r"api\.openai\.com", r"api\.anthropic\.com", r"11434"])
def test_tools_plugins_writer_cannot_reach_a_language_model(pattern):
    offenders = [
        str(p.relative_to(ROOT))
        for p in _source_files("tools", "plugins", "writer", "db", "dashboard")
        if re.search(pattern, p.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"{pattern!r} found in {offenders}: only core.graph may call the LLM client"


def test_only_the_graph_and_the_cli_import_the_llm_client():
    files = [*_source_files(*APP_DIRS), ROOT / "main.py", ROOT / "config.py"]
    importers = sorted(
        str(p.relative_to(ROOT)) for p in files if re.search(r"from core\.llm import|import core\.llm", p.read_text(encoding="utf-8"))
    )
    assert importers == ["core/graph.py", "main.py"]
