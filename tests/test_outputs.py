"""The output pipeline for the asteroid case, and the pieces every case shares.

Upload files and a Markdown mission log, never a paper: anomaly screening, submission statuses and files, the mission
log, artifact rows, the whole graph, and the asteroid plugin's own ADES drafts and target lists.  The Rubin and transit cases are in ``test_outputs_rubin.py`` and ``test_outputs_transit.py``
(split so they can run side by side; see ``tests/conftest.py`` ``run_once``).
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from core import anomaly
from core.base_plugin import BaseExperimentPlugin, PluginContractError
from core.mission_log import build_log
from core.submission import (
    STATUS_DRAFT, STATUS_HELD, STATUS_READY, STATUS_REPLAY, STATUS_SIMULATED, SubmissionDraft, classify, summarize, write_submissions,
)
from plugins import load_plugin
from tests.conftest import run_graph


# --------------------------------------------------------------------------- #
# Anomaly screening: the bar is enforced in core, not by plugins
# --------------------------------------------------------------------------- #
def _a(kind="timing_offset", sigma=6.0, target="T"):
    return {"kind": kind, "target": target, "summary": "s", "significance_sigma": sigma, "evidence": {"x": 1}}


def test_screen_splits_at_the_threshold_and_sorts_by_strength():
    accepted, below, problems = anomaly.screen([_a(sigma=5.0), _a(sigma=4.99), _a(sigma=9.0), _a(sigma=-7.0)], 5.0)
    assert [a["significance_sigma"] for a in accepted] == [9.0, -7.0, 5.0]  # |sigma| >= 5, strongest first
    assert [a["significance_sigma"] for a in below] == [4.99] and problems == []


@pytest.mark.parametrize("bad", [
    "not a dict", {"kind": "k"}, {**_a(), "significance_sigma": "high"},
    {**_a(), "significance_sigma": float("nan")}, {**_a(), "significance_sigma": float("inf")},
])
def test_screen_drops_malformed_proposals_and_says_why(bad):
    accepted, below, problems = anomaly.screen([bad, _a()], 5.0)
    assert len(accepted) == 1 and below == [] and len(problems) == 1


def test_screen_coerces_a_missing_evidence_dict():
    accepted, _, _ = anomaly.screen([{**_a(), "evidence": None}], 5.0)
    assert accepted[0]["evidence"] == {}


# --------------------------------------------------------------------------- #
# Submission files
# --------------------------------------------------------------------------- #
def _draft(name="a.txt", content="data\n", **kw):
    return SubmissionDraft(network="Net", kind="k", format="F", filename=name, content=content, target="T", **kw)


def test_classify_statuses():
    assert classify(_draft(content=None), simulated=False, data_source="telescope") == STATUS_HELD
    assert classify(_draft(content=None), simulated=True, data_source="telescope") == STATUS_HELD
    assert classify(_draft(), simulated=True, data_source="telescope") == STATUS_SIMULATED
    assert classify(_draft(), simulated=False, data_source="archive") == STATUS_REPLAY
    assert classify(_draft(blockers=["x"]), simulated=False, data_source="telescope") == STATUS_DRAFT
    assert classify(_draft(), simulated=False, data_source="telescope") == STATUS_READY


def test_files_are_prefixed_by_status_and_nothing_is_ever_marked_sent(tmp_path):
    drafts = [_draft("ok.txt"), _draft("needs.txt", blockers=["no code"]), _draft("none.txt", content=None, blockers=["why"])]
    recs = write_submissions(drafts, tmp_path, mission_id="m", simulated=False, data_source="telescope")
    assert [r["status"] for r in recs] == [STATUS_READY, STATUS_DRAFT, STATUS_HELD]
    assert [r["file"] for r in recs] == ["ok.txt", "DRAFT_needs.txt", None]
    assert (tmp_path / "ok.txt").read_text() == "data\n" and not (tmp_path / "none.txt").exists()
    assert all(r["sent"] is False for r in recs)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["files"][0]["sha256"] == recs[0]["sha256"] and "Nothing in this folder has been sent" in manifest["note"]
    assert summarize(recs) == {STATUS_SIMULATED: 0, STATUS_REPLAY: 0, STATUS_DRAFT: 1, STATUS_READY: 1, STATUS_HELD: 1}


def test_simulated_and_replayed_files_can_never_be_ready(tmp_path):
    sim = write_submissions([_draft()], tmp_path / "s", mission_id="m", simulated=True, data_source="telescope")[0]
    assert sim["file"] == "SIMULATED_a.txt" and sim["status"] == STATUS_SIMULATED and "never be uploaded" in sim["blockers"][-1]
    rep = write_submissions([_draft()], tmp_path / "r", mission_id="m", simulated=False, data_source="archive")[0]
    assert rep["file"] == "REPLAY_a.txt" and rep["status"] == STATUS_REPLAY and "already public" in rep["blockers"][-1]


def test_two_drafts_with_the_same_name_do_not_overwrite_each_other(tmp_path):
    recs = write_submissions([_draft(content="1\n"), _draft(content="2\n")], tmp_path, mission_id="m", simulated=False, data_source="telescope")
    assert recs[0]["file"] != recs[1]["file"]
    assert {(tmp_path / r["file"]).read_text() for r in recs} == {"1\n", "2\n"}


def test_a_hostile_filename_cannot_escape_the_folder(tmp_path):
    out = tmp_path / "sub"
    rec = write_submissions([_draft("../../evil name?.txt")], out, mission_id="m", simulated=False, data_source="telescope")[0]
    assert Path(rec["path"]).parent == out and ".." not in rec["file"] and "?" not in rec["file"]
    assert not (tmp_path / "evil name?.txt").exists()


def test_attachments_are_copied_and_a_missing_one_downgrades_the_file(tmp_path):
    img = tmp_path / "finder.png"
    img.write_bytes(b"png")
    out = tmp_path / "out"
    rec = write_submissions([_draft(attachments=[str(img)])], out, mission_id="m", simulated=False, data_source="telescope")[0]
    assert rec["status"] == STATUS_READY and (out / rec["attachments"][0]).read_bytes() == b"png"
    rec = write_submissions([_draft(attachments=[str(tmp_path / "gone.png")])], out, mission_id="m", simulated=False, data_source="telescope")[0]
    assert rec["status"] == STATUS_DRAFT and "attachment is missing" in rec["blockers"][0]


def test_rewriting_a_mission_is_safe(tmp_path):
    for _ in range(2):
        recs = write_submissions([_draft()], tmp_path, mission_id="m", simulated=False, data_source="telescope")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.txt", "manifest.json"] and recs[0]["file"] == "a.txt"


# --------------------------------------------------------------------------- #
# Mission log and artifacts
# --------------------------------------------------------------------------- #
def _mission(**over):
    m = {
        "mission_id": "m1", "plugin_name": "p", "night": "2026-10-21", "status": "COMPLETE", "hypothesis": "h",
        "simulated": False, "data_source": "telescope", "anomaly_min_sigma": 5.0,
        "targets": [{"name": "T1", "network": {"sources": ["exoclock"], "task": "ephemeris_refinement", "reason": "stale", "score": 3.2}},
                    {"name": "T2"}],
        "observations": [{"target": {"name": "T1"}, "exposure_plan": {"count": 10, "exposure_s": 5.0}, "findings": {"summary": "f"}, "crossref": {"summary": "c"}}],
        "anomalies": [], "below_threshold": [], "submissions": [], "errors": [],
    }
    m.update(over)
    return m


def _tables(doc):
    return {t["caption"]: t for s in doc["sections"] for t in s["tables"]}


def test_mission_log_names_the_network_task_and_flags_manual_targets():
    rows = _tables(build_log(_mission()))["Planned targets."]["rows"]
    assert rows[0][:3] == ["T1", "exoclock", "ephemeris refinement"] and rows[0][4] == "3.2"
    assert rows[1][1] == "operator / built-in" and rows[1][2] == "manual"


def test_mission_log_states_the_screening_result():
    quiet = build_log(_mission())
    text = json.dumps(quiet)
    assert "No anomaly reached the 5.0 sigma threshold" in text
    loud = build_log(_mission(
        anomalies=[{"kind": "outburst", "target": "T1", "summary": "bright", "significance_sigma": 8.2}],
        below_threshold=[{"kind": "timing_offset", "target": "T2", "summary": "meh", "significance_sigma": 3.1}],
    ))
    text = json.dumps(loud)
    assert "outburst on T1 (8.2 sigma)" in text and "Below the threshold" in text


def test_mission_log_never_overstates_a_simulation_or_a_send():
    sim = json.dumps(build_log(_mission(simulated=True, submissions=[
        {"file": "SIMULATED_a.txt", "network": "N", "format": "F", "status": STATUS_SIMULATED, "blockers": ["simulated data"]}])))
    assert "synthetic" in sim and "Nothing was sent anywhere" in sim and "0 ready for review" in sim
    assert "no upload files" in json.dumps(build_log(_mission()))


def test_mission_log_carries_the_credit_for_replayed_public_data():
    credit = "Data: a public survey set; thanks to the observatory that took it."
    obs = [{"target": {"name": "T1"}, "archive": {"credit": credit}}, {"target": {"name": "T2"}, "archive": {"credit": credit}}]
    doc = build_log(_mission(observations=obs, data_source="archive"))
    ack = [s for s in doc["sections"] if s["heading"] == "Acknowledgements"]
    assert len(ack) == 1 and ack[0]["paragraphs"] == [credit]  # once, however many visits used it
    assert "replay of public archive data" in json.dumps(doc)
    assert not [s for s in build_log(_mission())["sections"] if s["heading"] == "Acknowledgements"]


def test_artifact_rows_are_replaced_not_duplicated(tmp_path, settings):
    from db.database import Database

    db = Database(settings.db_path)
    try:
        db.create_mission("m", "p", True, {})
        db.replace_artifacts("m", "submission", [{"title": "a", "status": STATUS_DRAFT, "meta": {"k": 1}}, {"title": "b"}])
        db.replace_artifacts("m", "mission_log", [{"title": "log", "path": "/x.md"}])
        db.replace_artifacts("m", "submission", [{"title": "c"}])  # a node re-run after a crash
        assert [a["title"] for a in db.list_artifacts("m", "submission")] == ["c"]
        assert {a["title"] for a in db.list_artifacts("m")} == {"log", "c"}
        assert db.list_artifacts("other") == []
    finally:
        db.dispose()


def test_a_mission_produces_upload_files_and_a_markdown_log_and_no_paper(run_once):
    final, seq, arts, _ = run_once("asteroid_finder")  # the run the ADES-draft tests read too
    assert final["status"] == "COMPLETE" and final["anomalies"] == []
    assert seq[-3:] == ["screen_anomalies", "package_submission", "write_log"]
    assert "paper" not in final and "paper_decision" not in final
    log = Path(final["mission_log"]["path"])
    assert log.is_file() and log.suffix == ".md" and log.name.startswith("mission_log_")
    assert not list(log.parent.glob("*.pdf")) and not list(log.parent.glob("*.tex"))
    text = log.read_text()
    assert text.startswith("# ") and "## Summary" in text and "## Observing log" in text and "## Reproducibility" in text
    assert "asteroid_finder (DRAFT, not ready for use" in text  # a draft plugin says so in its own log
    assert len(arts["mission_log"]) == 1 and len(arts["submission"]) == len(final["submissions"]) == 3
    folder = Path(final["results_dir"])
    assert (folder / "manifest.json").is_file()
    assert log.parent == folder and Path(final["findings"]["path"]).parent == folder  # one folder to open
    assert all(r["sent"] is False and r["status"] in (STATUS_SIMULATED, STATUS_HELD) for r in final["submissions"])


def test_a_plugin_cannot_talk_itself_past_the_threshold(settings, monkeypatch):
    plugin_cls = type(load_plugin("asteroid_finder", settings))
    monkeypatch.setattr(plugin_cls, "detect_anomalies", lambda self, m: [_a("wishful", 4.9, "Field B (sim)")])
    final, _, _, _ = run_graph(settings, "asteroid_finder", anomaly_min_sigma=5.0, max_targets=1)
    assert final["anomalies"] == [] and len(final["below_threshold"]) == 1


def test_a_crashing_screen_or_packager_costs_only_its_own_output(settings, monkeypatch):
    plugin_cls = type(load_plugin("asteroid_finder", settings))

    def boom(self, m):
        raise RuntimeError("bug")

    monkeypatch.setattr(plugin_cls, "detect_anomalies", boom)
    monkeypatch.setattr(plugin_cls, "build_submissions", boom)
    final, _, _, _ = run_graph(settings, "asteroid_finder", max_targets=1)
    assert final["status"] == "COMPLETE" and len(final["observations"]) == 1  # the science survived
    assert final["submissions"] == [] and Path(final["mission_log"]["path"]).is_file()
    assert any("detect_anomalies failed" in e for e in final["errors"]) and any("build_submissions failed" in e for e in final["errors"])


def test_build_submissions_must_return_submission_drafts(settings, monkeypatch):
    plugin_cls = type(load_plugin("asteroid_finder", settings))
    monkeypatch.setattr(plugin_cls, "build_submissions", lambda self, m: ["not a draft"])
    final, _, _, _ = run_graph(settings, "asteroid_finder", max_targets=1)
    assert final["submissions"] == [] and any("SubmissionDraft" in e for e in final["errors"])


def test_the_base_plugin_defaults_are_empty():
    assert BaseExperimentPlugin.build_submissions(object(), {}) == []
    assert BaseExperimentPlugin.detect_anomalies(object(), {}) == []


def test_a_target_with_a_malformed_network_block_is_a_contract_error(settings, monkeypatch):
    plugin = load_plugin("asteroid_finder", settings)
    good = plugin.generate_targets()
    good[0]["network"] = {"sources": [], "task": "nonsense", "reason": ""}
    monkeypatch.setattr(plugin, "generate_targets", lambda: good)
    with pytest.raises(PluginContractError):
        plugin.validated_targets()


# --------------------------------------------------------------------------- #
# Asteroid: ADES drafts
# --------------------------------------------------------------------------- #
@pytest.fixture()
def asteroid_mission(run_once):
    final = run_once("asteroid_finder")[0]  # the same run as the mission-log test
    return {"night": "2026-10-21", "simulated": True, "data_source": "telescope", "observations": final["observations"]}


def _drafts(settings, mission, **over):
    return {d.target: d for d in _plugin(settings, "asteroid_finder", **over).build_submissions(mission)}


def test_only_an_identified_track_with_enough_positions_becomes_an_ades_file(settings, asteroid_mission):
    drafts = _drafts(settings, asteroid_mission, mpc_station_code="Z99", observer_name="A. Observer")
    eros = drafts["433 Eros (sim)"]
    assert eros.content and eros.blockers == [] and eros.format == "IAU ADES 2017 (PSV)"
    rows = [ln.split("|") for ln in eros.content.splitlines() if ln and ln[0] not in "#!" and not ln.startswith("permID")]
    assert len(rows) == 4 and {r[0] for r in rows} == {"433"} and {r[4] for r in rows} == {"Z99"} and {r[10] for r in rows} == {"Gaia3"}
    assert all(float(r[8]) >= 0.1 for r in rows)  # a floor on the stated uncertainty
    assert "! mpcCode Z99" in eros.content and "A. Observer" in eros.content
    field_b = drafts["Field B (sim)"]
    assert field_b.content is None and any("ask the MPC" in b for b in field_b.blockers)  # not identified: ask before sending
    control = drafts["Field C control (sim)"]
    assert control.content is None and "no moving object" in control.blockers[0]


def test_ades_draft_is_blocked_without_a_station_code_or_observer_name(settings, asteroid_mission):
    eros = _drafts(settings, asteroid_mission, mpc_station_code="", observer_name="")["433 Eros (sim)"]
    assert eros.content and len(eros.blockers) == 2
    assert any("MPC observatory code" in b for b in eros.blockers) and any("observer name" in b for b in eros.blockers)
    assert "mpcCode XXX" in eros.content  # a visible placeholder, and the draft is blocked until it is replaced


def test_ades_holds_astrometry_that_was_not_calibrated_against_gaia(settings, asteroid_mission):
    obs = json.loads(json.dumps(asteroid_mission["observations"]))
    eros = next(o for o in obs if o["target"]["name"] == "433 Eros (sim)")
    eros["crossref"]["calibration"]["status"] = "uncalibrated"
    draft = _plugin(settings, "asteroid_finder", mpc_station_code="Z99", observer_name="x").build_submissions(
        {**asteroid_mission, "observations": [eros]})[0]
    assert draft.content is None and "not calibrated against Gaia" in draft.blockers[0]
    eros["crossref"]["calibration"].update(status="survey_wcs")
    draft = _plugin(settings, "asteroid_finder").build_submissions({**asteroid_mission, "observations": [eros]})[0]
    assert draft.content is None and "survey" in draft.blockers[0]


def test_ades_refuses_an_unmapped_reference_catalogue_for_real_data(settings, asteroid_mission):
    obs = json.loads(json.dumps(asteroid_mission["observations"]))
    eros = next(o for o in obs if o["target"]["name"] == "433 Eros (sim)")
    live = _plugin(settings, "asteroid_finder", offline_services=False, mock_mode=True)
    live.simulated = False  # real data: the offline stand-in catalogue gets no ADES code
    draft = live.build_submissions({**asteroid_mission, "observations": [eros]})[0]
    assert draft.content is None and "has no ADES code" in draft.blockers[0]


def test_a_neocp_track_is_reported_under_its_temporary_designation_but_must_be_checked(settings, asteroid_mission):
    obs = json.loads(json.dumps(asteroid_mission["observations"]))
    field_b = next(o for o in obs if o["target"]["name"] == "Field B (sim)")
    field_b["target"]["neocp_desig"] = "P22abcd"
    draft = _plugin(settings, "asteroid_finder", mpc_station_code="Z99", observer_name="x").build_submissions(
        {**asteroid_mission, "observations": [field_b]})[0]
    assert draft.content
    rows = [ln.split("|") for ln in draft.content.splitlines() if ln and ln[0] not in "#!" and not ln.startswith("permID")]
    assert rows and all(r[0] == "" and r[1] == "" and r[2] == "P22abcd" for r in rows)
    assert any("has not been matched to P22abcd" in b for b in draft.blockers)  # so it is a draft, never ready
    assert any("temporary designation" in n for n in draft.notes)


def test_ades_designation_parsing():
    from plugins.asteroid_finder.plugin import AsteroidFinderPlugin as P

    assert P._ades_ids("433") == ("433", "") and P._ades_ids("2019 AB12") == ("", "2019 AB12")
    assert P._ades_ids("Eros") is None and P._ades_ids(None) is None and P._ades_ids("") is None


# --------------------------------------------------------------------------- #
# Asteroid and transit target selection from the networks
# --------------------------------------------------------------------------- #
def test_the_neocp_is_used_when_something_is_bright_enough(settings, monkeypatch):
    from core import networks
    from plugins.asteroid_finder import mpc

    row = {"Temp_Desig": "P22abcd", "R.A.": 6.5, "Decl.": 12.0, "V": 14.2, "Score": 80, "NObs": 12, "Arc": 1.2, "Not_Seen_dys": 0.4}
    monkeypatch.setattr(networks, "_http_get_text", lambda url, params, timeout: json.dumps([row]))
    monkeypatch.setenv("ASTRONOMO_ASTEROID_MIN_ALT_DEG", "-90")
    monkeypatch.setenv("ASTRONOMO_ASTEROID_TARGETS", "")
    plugin = load_plugin("asteroid_finder", replace(settings, offline_services=False, mock_mode=True))
    monkeypatch.setattr(plugin, "_known_asteroid_targets", lambda *a, **k: [])
    targets = plugin.generate_targets()
    assert [t["neocp_desig"] for t in targets] == ["P22abcd"] and targets[0]["designation"] is None
    net = targets[0]["network"]
    assert net["sources"] == [mpc.SOURCE] and net["task"] == "astrometry_followup" and "P22abcd" in net["reason"]


def test_a_neocp_with_nothing_bright_falls_back_to_the_labelled_built_in_list(settings, monkeypatch):
    from core import networks

    faint = [{"Temp_Desig": "P22faint", "R.A.": 6.5, "Decl.": 12.0, "V": 21.0, "Score": 80, "NObs": 5, "Arc": 0.3, "Not_Seen_dys": 0.2}]
    monkeypatch.setattr(networks, "_http_get_text", lambda url, params, timeout: json.dumps(faint))
    plugin = load_plugin("asteroid_finder", replace(settings, offline_services=False, mock_mode=True))
    seen = {}

    def fake_known(csv, label, why, *, raise_if_none):
        seen.update(csv=csv, label=label, raise_if_none=raise_if_none)
        return [{"name": "(4)", "network": {"sources": [label], "task": "manual", "reason": why}}]

    monkeypatch.setattr(plugin, "_known_asteroid_targets", fake_known)
    monkeypatch.delenv("ASTRONOMO_ASTEROID_TARGETS", raising=False)
    targets = plugin.generate_targets()
    assert seen["label"] == "built_in" and seen["raise_if_none"] is True
    assert targets[0]["network"]["task"] == "manual" and "no network target list" in targets[0]["network"]["reason"]


def test_an_operator_list_replaces_both_the_neocp_and_the_built_in_list(settings, monkeypatch):
    plugin = load_plugin("asteroid_finder", replace(settings, offline_services=False, mock_mode=True))
    monkeypatch.setenv("ASTRONOMO_ASTEROID_TARGETS", "4,433")
    monkeypatch.setattr(plugin, "_neocp_targets", lambda: pytest.fail("the NEOCP must not be consulted"))
    seen = {}
    monkeypatch.setattr(plugin, "_known_asteroid_targets", lambda csv, label, why, **k: seen.update(csv=csv, label=label) or [])
    plugin.generate_targets()
    assert seen == {"csv": "4,433", "label": "operator"}


def _plugin(settings, name, **over):
    return load_plugin(name, replace(settings, **over))
