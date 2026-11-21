"""The findings summary: one page per mission, in the one folder a person opens, carrying each plugin's caveats."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.base_plugin import BaseExperimentPlugin
from core.findings import GENERIC_CAVEATS, build_findings, collect_caveats, collect_rows
from plugins import load_plugin
from writer import write_findings

PLUGINS = ("variable_stars", "transit_finder", "asteroid_finder", "rubin_followup")
DEFAULT_CAVEAT = BaseExperimentPlugin.caveats(object())[0]


def _mission(**over):
    m = {
        "mission_id": "m1", "plugin_name": "p", "night": "2026-11-21", "status": "COMPLETE", "simulated": False,
        "data_source": "telescope", "anomalies": [],
        "observations": [{
            "target": {"name": "T1"}, "findings": {"summary": "f"}, "crossref": {"summary": "c"},
        }],
        "submissions": [{
            "file": "a.txt", "network": "N", "status": "draft", "blockers": ["no observer code"], "attachments": ["a__finder.png"],
        }],
        "setup": {},
    }
    m.update(over)
    return m


def _page(plugin, mission, tmp_path):
    rows, _ = collect_rows(plugin, mission["observations"])
    caveats, _ = collect_caveats(plugin)
    spec = build_findings(mission, rows=rows, caveats=caveats, log_name="mission_log_2026-11-21.md")
    return write_findings(spec, tmp_path, basename="findings_2026-11-21"), spec


def test_the_base_plugin_says_so_when_it_has_not_described_its_numbers():
    assert "has not stated what its numbers" in DEFAULT_CAVEAT
    notes = BaseExperimentPlugin.result_notes(object(), {"findings": {"summary": "f"}, "crossref": {"summary": "c"}})
    assert notes == {"measured": "f", "compared": "c"}


@pytest.mark.parametrize("name", PLUGINS)
def test_every_shipped_plugin_states_what_its_numbers_mean(name, settings, tmp_path):
    plugin = load_plugin(name, settings)
    own = plugin.caveats()
    assert own and DEFAULT_CAVEAT not in own  # a plugin may not stay silent
    assert len(set(own)) == len(own)
    result, _ = _page(plugin, _mission(), tmp_path)
    text = Path(result.path).read_text()
    for caveat in (*GENERIC_CAVEATS, *own):
        assert " ".join(caveat.split()) in text  # every caveat reaches the page, whole


def test_the_page_names_the_error_the_check_list_and_the_files(settings, tmp_path):
    result, _ = _page(load_plugin("variable_stars", settings), _mission(), tmp_path)
    text = Path(result.path).read_text()
    assert text.startswith("# Findings, 2026-11-21: p")
    for heading in ("## What was measured", "## Finder image", "## What the numbers mean", "## Check before sending", "## Files"):
        assert heading in text
    assert "| T1 | f | This plugin does not quote an error for this result. | c |" not in text  # the plugin words its own error
    assert "no observer code" in text and "a__finder.png" in text and "[mission_log_2026-11-21.md]" in text
    assert "No first-frame view was saved" in text


def test_a_simulation_and_an_anomaly_change_what_the_person_must_check(settings, tmp_path):
    plugin = load_plugin("variable_stars", settings)
    _, spec = _page(plugin, _mission(simulated=True, anomalies=[{"kind": "outburst"}]), tmp_path)
    assert spec["checks"][0].startswith("This was a simulation") and any("anomaly" in c for c in spec["checks"])
    assert spec["simulated"] is True


def test_the_finder_image_is_the_first_frame_beside_the_page(settings, tmp_path):
    preview = tmp_path / "first_frame.png"
    preview.write_bytes(b"png")
    out = tmp_path / "results"
    result, _ = _page(load_plugin("variable_stars", settings), _mission(setup={"preview_path": str(preview)}), out)
    assert (out / "figures" / "finder_first_frame.png").read_bytes() == b"png"
    assert "![" in Path(result.path).read_text() and "figures/finder_first_frame.png" in Path(result.path).read_text()


def test_a_plugin_that_raises_costs_only_its_own_row(settings):
    plugin = load_plugin("variable_stars", settings)

    def boom(observation):
        if observation["target"]["name"] == "bad":
            raise RuntimeError("bug")
        return {"measured": "ok"}

    plugin.result_notes = boom  # type: ignore[method-assign]
    rows, problems = collect_rows(plugin, [{"target": {"name": "good"}}, {"target": {"name": "bad"}}])
    assert rows[0]["measured"] == "ok" and rows[1]["measured"] == "--"
    assert len(problems) == 1 and "bad" in problems[0]


@pytest.mark.parametrize("name", ("variable_stars", "transit_finder"))
def test_a_mission_leaves_everything_in_one_folder(run_once, settings, name):
    final, _, arts, _ = run_once(name)
    folder = Path(final["results_dir"])
    names = {p.name for p in folder.iterdir()}
    assert "manifest.json" in names
    assert any(n.startswith("mission_log_") and n.endswith(".md") for n in names)
    page = Path(final["findings"]["path"])
    assert page.parent == folder and page.name.startswith("findings_")
    assert any(n.startswith("SIMULATED_") for n in names)  # the upload files are in the same folder
    assert not (folder.parent / "log").exists() and not (folder.parent / "submissions").exists()

    text = page.read_text()
    assert "## What was measured" in text and "Nothing was sent" in text
    for caveat in load_plugin(name, settings).caveats():
        assert " ".join(caveat.split()) in text  # the plugin's own words reach the page of a real mission run

    stored = arts["findings"]
    assert len(stored) == 1 and stored[0]["path"] == str(page)
    assert stored[0]["meta"]["rows"] and stored[0]["meta"]["caveats"] and stored[0]["meta"]["checks"]


def test_the_dashboard_payload_carries_the_findings_table_caveats_and_checks(settings, tmp_path):
    from dashboard.api.queries import artifacts_payload
    from db.database import Database

    page = tmp_path / "findings_2026-11-21.md"
    page.write_text("# Findings")
    db = Database(settings.db_path)
    try:
        db.create_mission("m", "variable_stars", True, {})
        db.replace_artifacts("m", "findings", [{
            "title": "Findings", "path": str(page), "status": "written",
            "meta": {"rows": [{"target": "T", "measured": "m", "error": "e", "compared": "c"}], "caveats": ["a"], "checks": ["b"]},
        }])
        found = artifacts_payload(db, "m")["findings"]
    finally:
        db.dispose()
    assert len(found) == 1 and found[0]["downloadable"] and found[0]["filename"] == page.name
    assert found[0]["rows"][0]["error"] == "e" and found[0]["caveats"] == ["a"] and found[0]["checks"] == ["b"]


def test_the_variable_star_page_quotes_the_error_and_what_it_includes(run_once):
    final, _, _, _ = run_once("variable_stars")
    text = Path(final["findings"]["path"]).read_text()
    assert "V1234 Lac (sim)" in text and "+/- " in text and "systematic floor" in text
    assert "Gaia G scale" in text and "usable night, not an eclipse" in text
